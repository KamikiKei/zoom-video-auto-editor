"""エディタ (app.py) との連携。

パイプラインが app.py の jobs/ 構造にジョブを作成し、動画・VTT・
OP/ED素材・カット案 (plan.json)・話者解析 (diarization.json) を
事前投入する。人間はブラウザで {EDITOR_BASE_URL}/?job=<job_id> を開けば、
アップロード済みの状態から微修正 → 書き出しができる。

ジョブフォルダの構成 (app.py の手動フローと同一):
  jobs/<job_id>/input.mp4        動画
  jobs/<job_id>/transcript.vtt   Zoom文字起こし
  jobs/<job_id>/thumb_asset      OP/ED素材 (画像 or 動画)
  jobs/<job_id>/plan.json        edit.py のカット案
  jobs/<job_id>/diarization.json 話者解析結果 (エディタのタイムライン用)
  jobs/<job_id>/samples/         話者ごとの試聴サンプル mp3
  jobs/<job_id>/jobmeta.json     付随情報 (タイトル・素材ファイル名等)
  jobs/<job_id>/output.mp4       書き出し結果 (エディタ or デーモンが生成)
"""
import json
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# パイプライン標準の編集設定 (edit.py compute_plan / render_plan の cfg)
DEFAULT_CFG = {
    "silence_cut": True,   # 3秒以上の無音をカット
    "silence_sec": 3.0,
    "silence_db": -38,
    "dissolve": 0.8,
    "intro_sec": 3,
    "outro_sec": 3,
}


def jobs_dir() -> Path:
    return REPO_ROOT / "jobs"


def create_editor_job(video: Path, transcript: Path | None = None,
                      asset: Path | None = None, cfg: dict | None = None,
                      meta: dict | None = None,
                      llm_client=None) -> tuple[str, Path, dict]:
    """ジョブを作成し (job_id, job_dir, plan) を返す。

    開始点は VTT の登壇者推定 (edit.compute_plan) が自動で入れる。
    デフォルト保持ポリシー (登壇者以外はカット候補) は compute_plan 側で
    最初から効く。llm_client が渡されれば LLM検出 (もたつき・トラブル)
    の候補も付き、結果は plan["llm_detection"] に記録される。
    素材が無い場合は plan の asset が None になり、書き出し時に
    エディタ側で素材を指定する必要がある。
    """
    import edit as edit_mod

    cfg = {**DEFAULT_CFG, **(cfg or {})}
    job_id = uuid.uuid4().hex[:12]
    d = jobs_dir() / job_id
    d.mkdir(parents=True, exist_ok=True)

    input_video = d / "input.mp4"
    shutil.copy2(video, input_video)
    vtt = None
    if transcript is not None:
        vtt = d / "transcript.vtt"
        shutil.copy2(transcript, vtt)
    thumb = None
    if asset is not None:
        thumb = d / "thumb_asset"
        shutil.copy2(asset, thumb)

    plan = edit_mod.compute_plan(input_video, cfg,
                                 transcript_path=vtt, thumb=thumb,
                                 llm_client=llm_client)
    (d / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # エディタのタイムライン表示用に話者解析も事前投入する
    if vtt is not None:
        try:
            from transcript import result_from_transcript
            from diarize import export_samples
            result = result_from_transcript(vtt)
            result["samples"] = export_samples(input_video, result, d / "samples")
            (d / "diarization.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  話者解析の事前投入に失敗 (続行します): {e}")

    jobmeta = dict(meta or {})
    jobmeta["source"] = "pipeline"
    if asset is not None:
        jobmeta["asset_name"] = Path(asset).name
    (d / "jobmeta.json").write_text(
        json.dumps(jobmeta, ensure_ascii=False, indent=2), encoding="utf-8")
    return job_id, d, plan


def editor_url(base_url: str, job_id: str) -> str:
    return f"{base_url.rstrip('/')}/?job={job_id}"


def render_job(job_id: str, cfg: dict | None = None) -> Path:
    """ジョブの plan.json から書き出す (エディタの「編集する」と等価)。

    デモモードで「人間がそのまま書き出しを押した」状態を再現するために使う。
    cfg では品質系 (preset 等) のみ上書きする。
    """
    import edit as edit_mod

    d = jobs_dir() / job_id
    plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    output = d / "output.mp4"
    edit_mod.render_plan(plan, output, cfg=cfg or {})
    return output


def plan_summary(plan: dict) -> str:
    """確認依頼メッセージ用の、カット案の要約 (1〜2行)。"""
    def fmt(s):
        s = int(round(float(s)))
        return f"{s // 60}:{s % 60:02d}"

    parts = [f"開始点 {fmt(plan['trim_start'])}"
             f"（出所: {plan.get('trim_start_source', '?')}）"]
    sp = plan.get("speakers") or {}
    names = sp.get("names") or {}
    presenters = plan.get("presenters") or []
    pnames = [names[str(p)] for p in presenters if str(p) in names]
    if pnames:
        parts.append("登壇者: " + "・".join(pnames))
    cuts = (plan.get("silence") or {}).get("cuts") or []
    if cuts:
        removed = sum(e - s for s, e in cuts)
        parts.append(f"カット {len(cuts)}箇所 (合計 {removed:.0f}秒)")
    cands = (plan.get("silence") or {}).get("candidates") or []
    by_src = {}
    for c in cands:
        if c.get("enabled"):
            by_src[c.get("source", "?")] = by_src.get(c.get("source", "?"), 0) + 1
    labels = {"silence": "無音", "ops": "運営", "other": "登壇者以外",
              "llm": "AI検出", "user": "手動"}
    if by_src:
        parts.append("候補内訳: " + "・".join(
            f"{labels.get(k, k)}{v}件" for k, v in sorted(by_src.items())))
    llm = plan.get("llm_detection") or {}
    if llm.get("status") == "failed":
        parts.append("AI検出は失敗したため候補なしで続行")
    parts.append(f"想定の長さ {fmt(plan['expected_duration'])}")
    return " / ".join(parts)
