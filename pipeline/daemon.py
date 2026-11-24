#!/usr/bin/env python3
"""動画編集パイプラインのデーモン (骨格)。

1回の実行 (run_once) で行うこと:
  1. 「確認待ち」のジョブに output.mp4 があれば (＝人間がエディタで
     書き出し済み) 、概要欄生成 → YouTube非公開アップロード →
     確認依頼② を送って完了にする
  2. 新着録画があれば、取り込み → エディタジョブ作成 (カット案まで)
     → 確認依頼① を送って「確認待ち」にする

通常運用では --poll で常駐させるか、cron 等から --once を定期実行する。
人間の書き出しを待つ間は止まらず、次回実行で続きを拾う設計。

--demo モード: 確認依頼①の直後に「人間がそのまま書き出しを押した」
状態を自動で再現し、1回の実行で全工程を通す (動作確認用)。

使い方:
  python3 -m pipeline.daemon --once            # モックで1回実行
  python3 -m pipeline.daemon --once --demo     # 全工程を通すデモ
  python3 -m pipeline.daemon --poll --interval 300

環境変数:
  PIPELINE_INBOX   モック用 inbox (既定: pipeline/inbox)
  PIPELINE_OUTBOX  モックYouTubeの出力先 (既定: pipeline/outbox)
  PIPELINE_STATE   状態JSON (既定: pipeline/state.json)
  PIPELINE_WORK    ダウンロード作業領域 (既定: pipeline/work)
  EDITOR_BASE_URL  エディタのURL (既定: http://localhost:8765)
  SLACK_WEBHOOK_URL あれば Webhook 通知、無ければコンソール通知
  ZOOM_CLIENT_ID   あれば Zoom 実接続 (未実装スタブ)
  YOUTUBE_CLIENT_SECRETS あれば YouTube 実接続 (未実装スタブ)
  GEMINI_API_KEY / GOOGLE_API_KEY あれば LLM検出 (もたつき・トラブル) が有効
  GEMINI_MODEL     LLM検出のモデル (既定 gemini-3-flash-preview)
  LLM_DETECT       "mock" でモック検出、"off" でLLM検出を無効化
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_detect  # noqa: E402
from pipeline import description, editor_bridge  # noqa: E402
from pipeline.notify import get_notifier  # noqa: E402
from pipeline.state import State  # noqa: E402
from pipeline.youtube_client import get_youtube_client  # noqa: E402
from pipeline.zoom_client import get_zoom_client  # noqa: E402

BASE = Path(__file__).resolve().parent


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


class Pipeline:
    def __init__(self, *, inbox: Path, outbox: Path, state_path: Path,
                 work_dir: Path, editor_base_url: str, demo: bool = False):
        self.state = State(state_path)
        self.zoom = get_zoom_client(inbox)
        self.youtube = get_youtube_client(outbox)
        self.notifier = get_notifier()
        self.work_dir = Path(work_dir)
        self.editor_base_url = editor_base_url
        self.demo = demo
        # LLM検出クライアント (キーが無ければ None = スキップ。
        # デモではモックになり、候補が付くところまで通る)
        self.llm_client = llm_detect.get_client_from_env(demo=demo)
        print(f"[pipeline] LLM検出: "
              f"{getattr(self.llm_client, 'name', 'なし (スキップ)')}")

    # -- 工程10〜11: 書き出し済みジョブの後段 --
    def _finish_job(self, rec_id: str, rec_state: dict) -> None:
        job_id = rec_state["job_id"]
        output = editor_bridge.jobs_dir() / job_id / "output.mp4"
        meta = rec_state.get("meta") or {}
        title = description.build_title(meta)
        if title is None:
            # 特別回などタイトル形式が決められないケースは推測で進めない
            self.notifier.notify_error(
                rec_state.get("title") or rec_id,
                "タイトルの形式を自動で決められませんでした (イベント回?)。"
                "meta に title_override を設定して再実行してください")
            self.state.put(rec_id, status="error", error="title_undecided")
            return
        desc = description.build_description(meta)
        result = self.youtube.upload_private(output, title, desc)
        summary = (f"長さ: {rec_state.get('expected_duration', '?')}秒想定 / "
                   f"ジョブ: {job_id}")
        self.notifier.notify_review2(title, result.url, summary)
        self.state.put(rec_id, status="done", youtube_url=result.url,
                       youtube_video_id=result.video_id, title=title)
        print(f"[pipeline] 完了: {rec_id} -> {result.url}")

    # -- 工程2〜6: 新着録画の前段 --
    def _start_recording(self, rec) -> None:
        print(f"[pipeline] 新着録画: {rec.id} ({rec.title or '無題'})")
        local = self.zoom.download(rec, self.work_dir / rec.id)
        meta = dict(local.meta)
        meta.setdefault("title", local.title)
        if local.asset is None:
            self.notifier.notify_error(
                local.title or rec.id,
                "OP/ED素材が特定できませんでした。素材を用意して再実行してください")
            self.state.put(rec.id, status="error", error="asset_missing",
                           title=local.title, meta=meta)
            return
        job_id, _job_dir, plan = editor_bridge.create_editor_job(
            local.video, transcript=local.transcript, asset=local.asset,
            meta=meta, llm_client=self.llm_client)
        url = editor_bridge.editor_url(self.editor_base_url, job_id)
        summary = editor_bridge.plan_summary(plan)
        title_for_msg = description.build_title(meta) or local.title or rec.id
        self.notifier.notify_review1(title_for_msg, url, summary)
        self.state.put(rec.id, status="awaiting_review", job_id=job_id,
                       editor_url=url, title=local.title, meta=meta,
                       expected_duration=plan["expected_duration"])
        print(f"[pipeline] 確認待ちにしました: job={job_id} {url}")
        if self.demo:
            # 人間がエディタでそのまま「編集する」を押した状態を再現
            print("[pipeline] (demo) 書き出しを自動で再現します")
            editor_bridge.render_job(job_id, cfg={"preset": "veryfast"})

    def run_once(self) -> None:
        # 1. 確認待ちジョブの後段 (output.mp4 ができていれば進める)
        for rec_id, rec_state in self.state.awaiting_review():
            output = (editor_bridge.jobs_dir() / rec_state["job_id"]
                      / "output.mp4")
            if output.exists():
                try:
                    self._finish_job(rec_id, rec_state)
                except Exception as e:  # noqa: BLE001
                    self.notifier.notify_error(rec_state.get("title") or rec_id,
                                               f"後段処理で失敗: {e}")
                    self.state.put(rec_id, status="error", error=str(e)[:300])
            else:
                print(f"[pipeline] 確認待ち (書き出し未完): {rec_id} "
                      f"job={rec_state['job_id']}")
        # 2. 新着録画の前段
        for rec in self.zoom.list_recordings():
            if self.state.get(rec.id) is not None:
                continue  # 処理済み・処理中は再処理しない
            try:
                self._start_recording(rec)
            except Exception as e:  # noqa: BLE001
                self.notifier.notify_error(rec.title or rec.id,
                                           f"前段処理で失敗: {e}")
                self.state.put(rec.id, status="error", error=str(e)[:300])
        # デモモードでは、今回立てたジョブもそのまま後段まで通す
        if self.demo:
            for rec_id, rec_state in self.state.awaiting_review():
                output = (editor_bridge.jobs_dir() / rec_state["job_id"]
                          / "output.mp4")
                if output.exists():
                    self._finish_job(rec_id, rec_state)


def main():
    ap = argparse.ArgumentParser(description="動画編集パイプライン デーモン")
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    ap.add_argument("--poll", action="store_true", help="定期実行を繰り返す")
    ap.add_argument("--interval", type=int, default=300, help="--poll の間隔秒 (既定300)")
    ap.add_argument("--demo", action="store_true",
                    help="人間の書き出しを自動で再現し、全工程を1回で通す")
    ap.add_argument("--inbox", help="モック用 inbox (既定: 環境変数 or pipeline/inbox)")
    ap.add_argument("--outbox", help="モックYouTube出力先")
    ap.add_argument("--state", help="状態JSONのパス")
    ap.add_argument("--work", help="ダウンロード作業領域")
    ap.add_argument("--editor-base-url",
                    help="エディタのURL (既定: 環境変数 or http://localhost:8765)")
    args = ap.parse_args()

    pipe = Pipeline(
        inbox=Path(args.inbox) if args.inbox else _env_path("PIPELINE_INBOX", BASE / "inbox"),
        outbox=Path(args.outbox) if args.outbox else _env_path("PIPELINE_OUTBOX", BASE / "outbox"),
        state_path=Path(args.state) if args.state else _env_path("PIPELINE_STATE", BASE / "state.json"),
        work_dir=Path(args.work) if args.work else _env_path("PIPELINE_WORK", BASE / "work"),
        editor_base_url=args.editor_base_url or os.environ.get(
            "EDITOR_BASE_URL", "http://localhost:8765"),
        demo=args.demo,
    )
    if args.poll:
        print(f"[pipeline] poll 開始 (間隔 {args.interval}s)。Ctrl+C で停止")
        while True:
            pipe.run_once()
            time.sleep(args.interval)
    else:
        pipe.run_once()


if __name__ == "__main__":
    main()
