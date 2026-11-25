#!/usr/bin/env python3
"""
Zoomアーカイブ動画 自動編集パイプライン

やること:
  1. 本編の頭 (OP) とお尻 (ED) をカット
  2. 頭とお尻に Canvaサムネイル画像を静止画クリップとして付ける
  3. つなぎ目すべてにディゾルブ (映像=xfade / 音声=acrossfade) を適用
  4. mp4 (H.264 + AAC) で書き出す

使い方:
  # 設定ファイルで1本
  python3 edit.py --config config.json

  # コマンドラインで1本
  python3 edit.py input.mp4 thumb.png -o output.mp4 \
      --trim-start 12 --trim-tail 8 \
      --intro-sec 3 --outro-sec 3 --dissolve 0.8

  # フォルダ内まとめて (input/*.mp4 + input/thumb.png -> output/)
  python3 edit.py --batch --config config.json

  # カット案だけを作る (書き出さない)。Zoom文字起こしがあれば開始点候補も入る
  python3 edit.py plan --video input.mp4 --transcript meeting.vtt \
      --thumb thumb.png --silence-cut --out plan.json

  # カット案から書き出す
  python3 edit.py render --plan plan.json -o output.mp4

OP/EDの切り方:
  --trim-start : 頭から何秒カットするか
  --trim-tail  : 末尾から何秒カットするか
  --end        : こちらを直接指定も可 (秒 or HH:MM:SS)。--trim-tailより優先

エンコーダ:
  cfg の "encoder" で "auto" (既定) / "x264" / "nvenc" を選べる。
  auto のときは h264_nvenc が実際に使えるか小さな試しエンコードで確認し、
  使えれば NVENC、だめなら libx264 にフォールバックする。

書き出し方式:
  既定は1パス (無音カットの保持区間も含め単一フィルタグラフで1回だけ
  エンコード)。保持区間が多い場合はメモリ安全な統合分割方式 (下記) を
  使い、失敗した場合は従来の中間ファイル方式 (2パス) に自動で
  フォールバックする。cfg "one_pass": false で従来方式を強制できる。

  統合分割方式: render 全体 (OP + 本編の保持区間 + ED) を1つの断片計画
  として組み立て、「本体断片 + つなぎ目断片」を個別にエンコードして
  concat で連結する。本編全体を通す再エンコードは走らない (従来方式の
  最終パスは本編を丸ごと再エンコードしていた)。断片の音声は PCM で
  書き、AAC プライミングによる音声ズレを防ぐ (ddcf66b の対策を継承)。

作業ファイル:
  中間ファイル (断片・本編カット) は作業ディレクトリに置く。場所は
  cfg "workdir" > 環境変数 EDIT_WORKDIR > システムの一時領域 の順
  (一時領域の空きが断片の必要量に足りない場合は出力先のフォルダへ
  フォールバックする。小さな tmpfs の /tmp 等での容量不足を避けるため)。
  入力 (元動画・素材) が作業ディレクトリと別ファイルシステム (Google
  Drive の FUSE 等) にある場合は、レンダー開始時に作業ディレクトリへ
  1回だけコピーしてから処理する (断片ごとの低速なリモートアクセスを
  避けるため)。完成品だけを指定の出力パスへ書く。

NVDEC:
  cfg "hwaccel": "cuda" で、NVENC 使用時に入力デコードへ -hwaccel cuda
  を付ける (既定は無効)。全フレームが CPU フィルタ (trim/scale/xfade)
  を通る構成のため、フレーム転送コストで逆効果になりやすく、実測で
  速くなる環境でのみ有効化すること。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def probe(path: Path) -> dict:
    """ffprobe は使わず、ffmpeg -i の出力から必要な情報だけ取る。
    (Windows の Smart App Control が、未使用の実行ファイル ffprobe を
     ブロックして WinError 4551 になる問題の回避)"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    )
    text = result.stderr or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if not m:
        raise RuntimeError(f"動画の長さを取得できませんでした: {path}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    streams = []
    for line in text.splitlines():
        if "Stream" not in line:
            continue
        if "Video:" in line:
            dm = re.search(r"(\d{2,5})x(\d{2,5})", line)
            fm = re.search(r"([\d.]+)\s*fps", line)
            fps = float(fm.group(1)) if fm else 30.0
            streams.append({
                "codec_type": "video",
                "width": int(dm.group(1)) if dm else 1920,
                "height": int(dm.group(2)) if dm else 1080,
                "r_frame_rate": f"{fps}/1",
            })
        elif "Audio:" in line:
            streams.append({"codec_type": "audio"})
    if not any(s["codec_type"] == "video" for s in streams):
        raise RuntimeError(f"映像ストリームが見つかりません: {path}")
    return {"streams": streams, "format": {"duration": str(duration)}}


def parse_time(v) -> float:
    """秒数 or 'HH:MM:SS' / 'MM:SS' を秒(float)に変換"""
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def _mmss(sec) -> str:
    """秒を m:ss 表記にする (進捗・要約表示用)"""
    sec = int(round(float(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def _progress(msg: str):
    """処理の途中経過を標準出力に1行ずつ出す。
    長い処理でも「止まって見える」ことがないよう、各段階で呼ぶ。
    呼び出し側 (Colab ノートブック等) がリアルタイムに表示できるよう
    必ず flush する。"""
    print(msg, flush=True)


# ffmpeg の stderr から「本当のエラー行」を拾うためのパターン。
# 失敗時に末尾数百文字だけ見ても原因が分からない (ストリーム記述の断片しか
# 残らない) ことがあったため、エラーらしい行を別途抽出して例外に含める。
_FFMPEG_ERROR_PATTERNS = (
    "error", "invalid", "failed", "failure", "matches no streams",
    "could not", "cannot", "no such file", "permission denied",
    "conversion failed", "not divisible", "does not match",
    "no stream", "undefined", "killed",
)


def _ffmpeg_error_summary(err_text: str, returncode: int) -> str:
    """ffmpeg の stderr 全文から、例外メッセージ用の要約を作る。
    エラー行 (最大10行) + 末尾1500文字 + 終了コードのヒントを含む。"""
    lines = [ln.strip() for ln in err_text.splitlines() if ln.strip()]
    err_lines = []
    for ln in lines:
        low = ln.lower()
        if any(p in low for p in _FFMPEG_ERROR_PATTERNS):
            if ln not in err_lines:
                err_lines.append(ln[:300])
        if len(err_lines) >= 10:
            break
    tail = err_text.strip()[-1500:]
    parts = [f"ffmpeg が失敗しました (終了コード {returncode})"]
    if returncode == -9:
        parts.append("SIGKILL で終了しました"
                     "（メモリ不足で OS に kill された可能性が高いです）")
    elif returncode is not None and returncode < 0:
        parts.append(f"シグナル {-returncode} で終了しました")
    if err_lines:
        parts.append("エラー行:\n" + "\n".join(err_lines))
    parts.append("出力の末尾:\n" + tail)
    return "\n".join(parts)


def _run_ffmpeg(cmd, expected, progress_cb=None, base_pct=0, span_pct=100):
    """ffmpeg を進捗コールバック付きで実行する"""
    cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    err_lines = []

    def drain_err():
        for ln in proc.stderr:
            err_lines.append(ln)
            # エラー行が末尾から押し出されないよう多めに保持する
            if len(err_lines) > 400:
                del err_lines[:200]

    import threading
    t = threading.Thread(target=drain_err, daemon=True)
    t.start()
    for ln in proc.stdout:
        ln = ln.strip()
        if ln.startswith(("out_time_us=", "out_time_ms=")):
            try:
                us = float(ln.split("=", 1)[1])
            except ValueError:
                continue
            if progress_cb and expected > 0:
                pct = base_pct + span_pct * (us / 1e6 / expected)
                progress_cb(max(0, min(99, int(pct))))
        elif ln == "progress=end" and progress_cb:
            progress_cb(min(100, base_pct + span_pct))
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        err_text = "".join(err_lines)
        print(err_text.strip()[-1500:], file=sys.stderr)
        raise RuntimeError(_ffmpeg_error_summary(err_text, proc.returncode))


# ---------------------------------------------------------------------------
# エンコーダ抽象化 (libx264 / NVENC)
# ---------------------------------------------------------------------------
_NVENC_CACHE = {}


def _nvenc_available() -> bool:
    """h264_nvenc が「一覧にある」だけでなく「実際にエンコードできる」かを確認する。
    GPUの無い環境では一覧にあっても試しエンコードが失敗するので、必ず実働確認する。"""
    if "ok" in _NVENC_CACHE:
        return _NVENC_CACHE["ok"]
    ok = False
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=30)
        if "h264_nvenc" in (r.stdout or ""):
            t = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=black:s=256x256:r=30:d=0.5",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, timeout=60)
            ok = t.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    _NVENC_CACHE["ok"] = ok
    return ok


def _encoder_args(cfg):
    """cfg から (ffmpegの映像エンコーダ引数, 表示名) を返す。

    encoder: "auto" (既定) / "x264" / "nvenc"
      - auto: NVENCが実働すれば NVENC、だめなら libx264
      - nvenc: 実働しなければ RuntimeError (明示指定なので黙って落とさない)
    NVENC時は libx264 crf 18〜20 相当を狙い -preset p4 / -rc vbr / -cq 21 を使う。
    """
    enc = str(cfg.get("encoder", "auto")).lower()
    if enc not in ("auto", "x264", "nvenc"):
        raise ValueError(f"encoder の値が不正です: {enc} (auto / x264 / nvenc)")
    if enc == "nvenc" and not _nvenc_available():
        raise RuntimeError(
            "h264_nvenc が使える環境ではありません (GPU・ドライバ・ffmpegを確認してください)")
    if enc == "x264" or (enc == "auto" and not _nvenc_available()):
        crf = int(cfg.get("crf", 18))
        preset = str(cfg.get("preset", "fast"))
        return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset], "libx264"
    cq = int(cfg.get("nvenc_cq", 21))
    npreset = str(cfg.get("nvenc_preset", "p4"))
    return (["-c:v", "h264_nvenc", "-preset", npreset,
             "-rc", "vbr", "-cq", str(cq), "-b:v", "0"], "h264_nvenc")


# ---------------------------------------------------------------------------
# 無音検出と保持区間
# ---------------------------------------------------------------------------
def detect_silences(path: Path, min_sec=3.0, db=-38):
    """min_sec 以上の無音区間 [(start, end), ...] (絶対時刻) を返す"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vn",
         "-af", f"silencedetect=noise={db}dB:d={min_sec}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences = []
    cur = None
    for ln in (result.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", ln)
        if m:
            cur = float(m.group(1))
        m = re.search(r"silence_end:\s*([\d.]+)", ln)
        if m and cur is not None:
            silences.append((cur, float(m.group(1))))
            cur = None
    return silences


def _keep_segments(silences, start, end, min_sec):
    """[start, end] 内の「無音でない区間」と「カットする無音区間」を返す"""
    cuts = []
    for s, e in silences:
        cs, ce = max(s, start), min(e, end)
        if ce - cs >= min_sec - 0.05:
            cuts.append((cs, ce))
    keeps = []
    cur = start
    for cs, ce in sorted(cuts):
        if cs > cur:
            keeps.append((cur, cs))
        cur = max(cur, ce)
    if cur < end:
        keeps.append((cur, end))
    return keeps, cuts


# ---------------------------------------------------------------------------
# デフォルト保持ポリシー (登壇者以外はカット候補にする)
# ---------------------------------------------------------------------------
# 名前に含まれていたら運営 (ホスト) らしいとみなす語。
# 実際のZoomアカウント名「松尾研 講義コミュニティ運営」もこれで拾える。
_OPS_NAME_HINTS = ("運営", "ホスト", "司会", "主催", "事務局", "スタッフ",
                   "コミュニティ", "host", "Host", "HOST", "MC")


def _policy_candidates(speakers, cues, presenters, start, end):
    """登壇者 (presenters) 以外の話者の発話区間をカット候補にする。

    候補は {"start", "end", "kind": "speaker", "source": "ops"|"other",
    "speaker", "speaker_name", "reason"} のリスト (enabled は呼び出し側)。
    運営 (ホスト) らしい話者は source="ops"、それ以外の非登壇者
    (質疑の参加者など) は source="other" に分ける。判定材料は:
      - 紹介発言 (ご紹介します等) をした話者 = 運営
      - 名前に運営らしい語 (_OPS_NAME_HINTS) を含む話者 = 運営
    同一話者の近接区間 (間隔2秒以内) は1つの候補にまとめる。
    """
    if not speakers or not presenters:
        return []
    names = speakers.get("names") or {}
    intro_names = set()
    if cues:
        from transcript import _INTRO_PATTERNS
        for c in cues:
            if c.get("speaker") and any(
                    p.search(c.get("text") or "") for p in _INTRO_PATTERNS):
                intro_names.add(c["speaker"])
    wanted = {int(p) for p in presenters}
    raw = []
    for seg in speakers.get("segments", []):
        sp = int(seg["speaker"])
        if sp in wanted:
            continue
        s = max(float(seg["start"]), float(start))
        e = min(float(seg["end"]), float(end))
        if e - s > 0.05:
            raw.append([s, e, sp])
    merged = []
    for s, e, sp in sorted(raw):
        if merged and merged[-1][2] == sp and s - merged[-1][1] <= 2.0:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, sp])
    out = []
    for s, e, sp in merged:
        name = names.get(str(sp)) or f"話者{sp + 1}"
        is_ops = name in intro_names or any(h in name for h in _OPS_NAME_HINTS)
        if is_ops:
            reason = f"運営（{name}）の発話 — デフォルトでは登壇者のみ残す"
        else:
            reason = f"登壇者以外の発話（{name}） — デフォルトでは登壇者のみ残す"
        out.append({
            "start": round(s, 3), "end": round(e, 3),
            "kind": "speaker", "source": "ops" if is_ops else "other",
            "speaker": sp, "speaker_name": name, "reason": reason,
        })
    return out


def _suggest_start(speakers, presenters):
    """指定された登壇者 (複数可) のうち、最初に話し始める位置を返す。

    まず「5秒以上続く発言」の頭を探し、無ければ最初の発言の頭。
    speakers は transcript / diarize の結果 dict (segments を含む)。
    """
    wanted = {int(p) for p in presenters}
    segs = [s for s in speakers.get("segments", [])
            if int(s["speaker"]) in wanted]
    if not segs:
        return None
    for s in segs:
        if float(s["end"]) - float(s["start"]) >= 5:
            return float(s["start"])
    return float(segs[0]["start"])


def _join_plan(keeps, dissolve, fps):
    """保持区間をつなぐ際の (ディゾルブ秒, xfade offset) のリストと、
    つないだ後の本編長を返す。短い区間ではディゾルブを自動で短くする
    (従来 _build_main_cut 内の計算と同一)。"""
    total = sum(b - a for a, b in keeps)
    joins = []
    if len(keeps) < 2 or dissolve <= 0:
        return joins, total
    acc = keeps[0][1] - keeps[0][0]
    for k in range(1, len(keeps)):
        prev_len = keeps[k - 1][1] - keeps[k - 1][0]
        seg_len = keeps[k][1] - keeps[k][0]
        d = min(dissolve, 0.45 * prev_len, 0.45 * seg_len)
        d = max(d, 1.0 / max(fps, 1))  # 最低1フレーム
        d = min(d, seg_len * 0.9, acc * 0.9)
        joins.append((d, acc - d))
        acc = acc + seg_len - d
    return joins, acc


def _scale_chain(width, height, fps):
    return (f"scale={width}:{height}:force_original_aspect_ratio=disable,"
            f"setsar=1,fps={fps},format=yuv420p")


def _render_chain2(inputs, segments, ds, width, height, fps, out_path,
                   venc, progress_cb, expected, base_pct=0, span_pct=100,
                   pcm_audio=False):
    """複数入力の区間列をディゾルブ (ds: つなぎ目ごとの秒数) でつないで
    out_path に書く単一グラフ方式。ds が空なら concat でつなぐ。
    xfade の全入力に settb=AVTB を入れ、ffmpeg のバージョン差による
    タイムベース不一致を防ぐ。

    inputs: 入力仕様のリスト。各要素は
      {"kind": "file",  "path": Path, "audio": bool, "seek": float,
       "hwaccel": bool}
      {"kind": "image", "path": Path, "audio": False}  (静止画ループ入力)
      "seek" は入力シーク秒 (その入力の segments はシーク後の時刻で渡す)。
      "audio" が False の区間は無音 (anullsrc) で埋める。
    segments: (入力インデックス, 開始秒, 終了秒) のリスト。
    venc: 映像エンコーダ引数 (例: libx264 の crf/preset や NVENC 引数)。
    注意: この方式は区間のフレームをグラフ内に溜めるため、メモリ使用量が
    区間数と長さに比例する。大きな plan では断片方式 (_build_unified /
    _build_main_cut_smart) を使う。"""
    scale = _scale_chain(width, height, fps)
    afmt = "aresample=48000,aformat=channel_layouts=stereo"
    if pcm_audio:
        afmt += ":sample_fmts=s16"
    filters = []
    for i, (idx, a, b) in enumerate(segments):
        filters.append(f"[{idx}:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,{scale},settb=AVTB[v{i}]")
        if inputs[idx].get("audio"):
            filters.append(f"[{idx}:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,{afmt}[a{i}]")
        else:
            filters.append(f"anullsrc=cl=stereo:r=48000,atrim=duration={b - a:.3f},"
                           f"asetpts=PTS-STARTPTS,{afmt}[a{i}]")
    if not ds:
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(segments)))
        filters.append(labels + f"concat=n={len(segments)}:v=1:a=1[v][a]")
    else:
        # xfade の offset は _join_plan と同じ蓄積計算
        offsets = []
        acc = segments[0][2] - segments[0][1]
        for k in range(1, len(segments)):
            offsets.append(acc - ds[k - 1])
            acc = acc + (segments[k][2] - segments[k][1]) - ds[k - 1]
        cur_v, cur_a = "v0", "a0"
        for k in range(1, len(segments)):
            d, off = ds[k - 1], offsets[k - 1]
            filters.append(f"[{cur_v}][v{k}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            filters.append(f"[vx{k}]settb=AVTB[vxs{k}]")
            filters.append(f"[{cur_a}][a{k}]acrossfade=d={d:.3f}[ax{k}]")
            cur_v, cur_a = f"vxs{k}", f"ax{k}"
        filters.append(f"[{cur_v}]null[v]")
        filters.append(f"[{cur_a}]anull[a]")
    cmd = ["ffmpeg", "-y"]
    for ii, spec in enumerate(inputs):
        if spec["kind"] == "image":
            cmd += ["-loop", "1", "-framerate", str(fps)]
            ends = [b for (i2, a, b) in segments if i2 == ii]
            if ends:
                cmd += ["-t", f"{max(ends) + 2.0:.3f}"]
            cmd += ["-i", str(spec["path"])]
        else:
            if spec.get("hwaccel"):
                cmd += ["-hwaccel", "cuda"]
            if spec.get("seek", 0.0) > 0:
                cmd += ["-ss", f"{spec['seek']:.3f}"]
            cmd += ["-i", str(spec["path"])]
    cmd += ["-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]",
            *venc, "-pix_fmt", "yuv420p"]
    if pcm_audio:
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [str(out_path)]
    _run_ffmpeg(cmd, expected, progress_cb, base_pct=base_pct, span_pct=span_pct)


def _render_chain(input_video, segments, ds, width, height, fps, out_path,
                  crf, preset, progress_cb, expected, base_pct=0, span_pct=100,
                  seek=0.0, pcm_audio=False, hwaccel=False):
    """単一入力版の _render_chain2 ラッパ (従来インタフェース互換)。
    seek > 0 のときは入力シークし、segments はシーク後の時刻で渡すこと。"""
    inputs = [{"kind": "file", "path": Path(input_video), "audio": True,
               "seek": seek, "hwaccel": hwaccel}]
    segs = [(0, a, b) for a, b in segments]
    venc = ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]
    _render_chain2(inputs, segs, ds, width, height, fps, out_path, venc,
                   progress_cb, expected, base_pct=base_pct, span_pct=span_pct,
                   pcm_audio=pcm_audio)


# 単一グラフ方式 (全区間を1つの ffmpeg で xfade 接続) が安全な上限。
# これを超えると、後ろの区間のフレームが xfade のキューに溜まり続け、
# メモリを使い切って ffmpeg が OS に kill される (実測: 1920x1080・
# 保持区間50個・本編 約76分で発生。1パス・2パスとも同じ死に方をした)。
_CHAIN_MAX_SEGMENTS = 8
_CHAIN_MAX_SECONDS = 20.0


def _needs_smart_render(keeps):
    """保持区間が多く/長い plan か (単一グラフ方式がメモリで死ぬ規模か)。"""
    total = sum(b - a for a, b in keeps)
    return len(keeps) > _CHAIN_MAX_SEGMENTS or total > _CHAIN_MAX_SECONDS


def _build_main_cut_smart(input_video, keeps, joins, width, height, fps,
                          out_path, crf, preset, progress_cb, main_dur,
                          workdir=None, hwaccel=False):
    """保持区間が多い場合の本編カット (メモリ安全な分割方式)。

    本編を「ボディ断片 (各区間から前後のトランジション分を除いた中身)」
    と「トランジション断片 (前区間の尻 d 秒 + 次区間の頭 d 秒を xfade
    した長さ d 秒のクリップ)」に分解し、各断片を個別の ffmpeg で書き
    出してから、concat デコーダで連結する (映像は stream copy、音声は
    再エンコード)。各グラフが扱うのは短い区間だけなので、区間が何十個・
    本編が何十分あってもメモリは断片サイズで有界になる。ボディ断片が
    短すぎる区間は、前後の区間とまとめて小さなチェーン (グループ) と
    して書き出す。

    断片の音声は PCM で書き出す。AAC で断片を書いて stream copy 連結
    すると、断片ごとのエンコード遅延 (プライミング) と末尾パディング
    (実測 約31ms/断片) が edit list ごと捨てられて実サンプルとして
    漏れ出し、音声だけが断片数に比例して長く (映像より遅く) なるため。
    PCM ならサンプル数が正確に一致し、連結時の再エンコードは1回で済む。
    断片の一時ファイルは workdir (無指定なら out_path の親) の下に作る。
    """
    n = len(keeps)
    lens = [b - a for a, b in keeps]
    # dbefore[k] = 区間 k の手前のつなぎ目のディゾルブ秒 (区間0は 0)
    dbefore = [0.0] + [d for d, _ in joins]
    min_body = max(0.12, 3.0 / max(fps, 1))

    # --- 区間をグループ化する ---
    # 既定は1区間=1グループ。ボディ (前後のトランジションを除いた長さ)
    # が短すぎる区間は直前のグループに寄せ、チェーンとして書き出す。
    groups = []
    for k in range(n):
        body_len = lens[k] - dbefore[k] - (dbefore[k + 1] if k < n - 1 else 0.0)
        if groups and body_len < min_body:
            groups[-1].append(k)
        else:
            groups.append([k])
    # 先頭グループが単独で短すぎる場合は次のグループと合体させる
    while len(groups) > 1:
        g = groups[0]
        out_len = (sum(lens[k] for k in g) - dbefore[g[0]]
                   - (dbefore[g[-1] + 1] if g[-1] < n - 1 else 0.0)
                   - sum(dbefore[k] for k in g[1:]))
        if out_len >= min_body:
            break
        groups[0] = g + groups.pop(1)

    # --- 描画ユニット列 (グループ本体 / グループ間のトランジション) ---
    units = []  # ("group", [seg...]) | ("trans", k)  k は次区間の index
    for gi, g in enumerate(groups):
        if gi > 0:
            units.append(("trans", g[0]))
        units.append(("group", g))

    tmpdir = Path(tempfile.mkdtemp(
        prefix="maincut_", dir=str(workdir if workdir is not None
                                   else out_path.parent)))
    try:
        atom_files = []
        done_span = 0.0
        total_span = max(main_dur, 0.1)

        def span_of(u):
            if u[0] == "trans":
                return dbefore[u[1]]
            g = u[1]
            return (sum(lens[k] for k in g) - dbefore[g[0]]
                    - (dbefore[g[-1] + 1] if g[-1] < n - 1 else 0.0)
                    - sum(dbefore[k] for k in g[1:]))

        for ui, u in enumerate(units):
            atom = tmpdir / f"atom_{ui:04d}.mkv"
            span = span_of(u)
            base = 30.0 * done_span / total_span
            pct_span = 30.0 * span / total_span
            if u[0] == "trans":
                k = u[1]
                d = dbefore[k]
                pa, pb = keeps[k - 1], keeps[k]
                segments = [(pa[1] - d, pa[1]), (pb[0], pb[0] + d)]
                ds = [d]
            else:
                g = u[1]
                segments = []
                for pos, k in enumerate(g):
                    a, b = keeps[k]
                    if pos == 0 and k > 0:
                        a = a + dbefore[k]
                    if pos == len(g) - 1 and k < n - 1:
                        b = b - dbefore[k + 1]
                    segments.append((a, b))
                ds = [dbefore[k] for k in g[1:]]
            # 断片の開始位置まで入力シークし、頭からのデコードし直しを避ける
            seek = max(0.0, min(a for a, _ in segments) - 0.5)
            if seek > 0:
                segments = [(a - seek, b - seek) for a, b in segments]
            _render_chain(input_video, segments, ds, width, height, fps,
                          atom, crf, preset, progress_cb, span,
                          base_pct=base, span_pct=pct_span, seek=seek,
                          pcm_audio=True, hwaccel=hwaccel)
            atom_files.append(atom)
            done_span += span

        # --- concat デコーダで連結 (映像 copy / 音声は AAC に再エンコード) ---
        list_path = tmpdir / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as f:
            for atom in atom_files:
                f.write(f"file '{str(atom).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)]
        _run_ffmpeg(cmd, main_dur, progress_cb, base_pct=30.0 * done_span / total_span,
                    span_pct=30.0 - 30.0 * done_span / total_span)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _build_main_cut(input_video, keeps, width, height, fps, out_path,
                    crf, preset, progress_cb, main_dur, dissolve=0.0,
                    workdir=None, hwaccel=False):
    """無音を除いた本編だけの中間ファイルを作る (従来方式・フォールバック用)。
    つなぎ目にはディゾルブ (xfade/acrossfade) をかける。
    保持区間が多い場合はメモリ安全な分割方式 (_build_main_cut_smart)。"""
    joins, _ = _join_plan(keeps, dissolve, fps)
    if joins and _needs_smart_render(keeps):
        _progress(f"保持区間が {len(keeps)} 個あるため、メモリ安全な分割方式で本編を作ります")
        _build_main_cut_smart(input_video, keeps, joins, width, height, fps,
                              out_path, crf, preset, progress_cb, main_dur,
                              workdir=workdir, hwaccel=hwaccel)
        return
    ds = [d for d, _ in joins]
    _render_chain(input_video, [tuple(k) for k in keeps], ds, width, height, fps,
                  out_path, crf, preset, progress_cb, main_dur,
                  base_pct=0, span_pct=30, hwaccel=hwaccel)


def _build_unified(plan, thumb, output, cfg, enc_args, progress_cb, workdir,
                   hwaccel=False):
    """統合分割方式: OP + 本編の保持区間 + ED を1つの断片計画として描く。

    クリップ列 (OP素材 / 保持区間 / ED素材) を「本体断片 (グループ)」
    と「つなぎ目断片」に分解し、各断片を最終エンコーダで個別に書き
    出して concat (映像 copy / 音声のみ AAC 化) で連結する。従来方式の
    最終パスと違い、本編全体を通す再エンコードは走らない。メモリの
    有界性と AAC プライミング対策 (断片音声は PCM) は _build_main_cut_smart
    と同じ仕組み。断片は workdir の下に作り、完成品だけを output に書く。

    端の保持区間がディゾルブより短い場合はつなぎ目断片を作れないため
    RuntimeError を投げ、呼び出し側で従来方式にフォールバックさせる。
    """
    vinfo = plan["video"]
    video = Path(vinfo["path"])
    width, height, fps = vinfo["width"], vinfo["height"], vinfo["fps"]
    has_audio = vinfo["has_audio"]
    keeps = [tuple(k) for k in plan["silence"]["keeps"]]
    dissolve = float(cfg.get("dissolve", 0.8))

    asset_type, asset_info = _resolve_asset(thumb, cfg)
    intro, outro, asset_has_audio = _asset_timing(asset_type, asset_info, cfg)
    if dissolve >= intro or dissolve >= outro:
        raise ValueError("ディゾルブ秒数がクリップ長より長いです。dissolveを短くしてください")

    joins, main_len = _join_plan(keeps, dissolve, fps)
    expected = float(plan.get("expected_duration")
                     or (intro + main_len + outro - 2 * dissolve))
    if dissolve > 0 and keeps and (keeps[0][1] - keeps[0][0] < dissolve
                                   or keeps[-1][1] - keeps[-1][0] < dissolve):
        raise RuntimeError("端の保持区間がディゾルブより短いため統合方式を使えません")

    # --- クリップ列と、各クリップの「手前のつなぎ目」秒数 ---
    clips = [{"src": "asset", "a": 0.0, "b": intro}]
    clips += [{"src": "video", "a": a, "b": b} for a, b in keeps]
    clips.append({"src": "asset", "a": 0.0, "b": outro})
    m = len(clips)
    if dissolve <= 0:
        dbefore = [0.0] * m
    else:
        dbefore = [0.0, dissolve] + [d for d, _ in joins] + [dissolve]
    lens = [c["b"] - c["a"] for c in clips]
    min_body = max(0.12, 3.0 / max(fps, 1))

    # --- グループ化 (_build_main_cut_smart と同じ方針) ---
    # 本体 (前後のつなぎ目を除いた長さ) が短すぎるクリップは直前の
    # グループに寄せ、小さなチェーンとして書き出す。チェーンは複数
    # 入力対応 (_render_chain2) なので、素材と本編にまたがってもよい。
    groups = []
    for k in range(m):
        body_len = lens[k] - dbefore[k] - (dbefore[k + 1] if k < m - 1 else 0.0)
        if groups and body_len < min_body:
            groups[-1].append(k)
        else:
            groups.append([k])
    while len(groups) > 1:
        g = groups[0]
        out_len = (sum(lens[k] for k in g) - dbefore[g[0]]
                   - (dbefore[g[-1] + 1] if g[-1] < m - 1 else 0.0)
                   - sum(dbefore[k] for k in g[1:]))
        if out_len >= min_body:
            break
        groups[0] = g + groups.pop(1)

    units = []  # ("group", [clip...]) | ("trans", 次クリップの index)
    for gi, g in enumerate(groups):
        if gi > 0:
            units.append(("trans", g[0]))
        units.append(("group", g))

    def span_of(u):
        if u[0] == "trans":
            return dbefore[u[1]]
        g = u[1]
        return (sum(lens[k] for k in g) - dbefore[g[0]]
                - (dbefore[g[-1] + 1] if g[-1] < m - 1 else 0.0)
                - sum(dbefore[k] for k in g[1:]))

    def unit_inputs_and_segments(u):
        """ユニット描画用の (inputs, segments, ds) を組み立てる。
        入力ごとに入力シークを決め、segments の時刻をシーク後に直す。"""
        if u[0] == "trans":
            k = u[1]
            d = dbefore[k]
            pc, nc = clips[k - 1], clips[k]
            raw = [(pc["src"], pc["b"] - d, pc["b"]),
                   (nc["src"], nc["a"], nc["a"] + d)]
            ds = [d] if d > 0 else []
        else:
            g = u[1]
            raw = []
            for pos, k in enumerate(g):
                a, b = clips[k]["a"], clips[k]["b"]
                if pos == 0 and k > 0:
                    a = a + dbefore[k]
                if pos == len(g) - 1 and k < m - 1:
                    b = b - dbefore[k + 1]
                raw.append((clips[k]["src"], a, b))
            ds = [dbefore[k] for k in g[1:] if dbefore[k] > 0]
            if len(ds) != len(g) - 1:
                # ディゾルブ0のつなぎ目が混ざる場合は concat 扱いにできない
                # (ds の位置対応がずれる) ため、すべて連結 (無音カット相当)
                ds = []
        order = []
        for src, a, b in raw:
            if src not in order:
                order.append(src)
        seeks = {}
        for src in order:
            if src == "video":
                seeks[src] = max(
                    0.0, min(a for s2, a, b in raw if s2 == src) - 0.5)
            else:
                seeks[src] = 0.0
        inputs = []
        for src in order:
            if src == "video":
                inputs.append({"kind": "file", "path": video,
                               "audio": has_audio, "seek": seeks[src],
                               "hwaccel": hwaccel})
            elif asset_type == "video":
                inputs.append({"kind": "file", "path": Path(thumb),
                               "audio": asset_has_audio, "seek": 0.0})
            else:
                inputs.append({"kind": "image", "path": Path(thumb),
                               "audio": False})
        index = {src: i for i, src in enumerate(order)}
        segments = [(index[src], a - seeks[src], b - seeks[src])
                    for src, a, b in raw]
        return inputs, segments, ds

    tmpdir = Path(tempfile.mkdtemp(prefix="unified_", dir=str(workdir)))
    try:
        atom_files = []
        done_span = 0.0
        total_span = max(expected, 0.1)
        for ui, u in enumerate(units):
            span = span_of(u)
            if span <= 0:
                continue  # ディゾルブ0のつなぎ目など、出力長0のユニット
            atom = tmpdir / f"atom_{ui:04d}.mkv"
            inputs, segments, ds = unit_inputs_and_segments(u)
            _render_chain2(inputs, segments, ds, width, height, fps, atom,
                           enc_args, progress_cb, span,
                           base_pct=92.0 * done_span / total_span,
                           span_pct=92.0 * span / total_span,
                           pcm_audio=True)
            atom_files.append(atom)
            done_span += span
        # --- concat で連結 (映像 copy / 音声は AAC に1回だけエンコード) ---
        list_path = tmpdir / "concat.txt"
        with open(list_path, "w", encoding="utf-8") as f:
            for atom in atom_files:
                f.write(f"file '{str(atom).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(output)]
        _run_ffmpeg(cmd, expected, progress_cb, base_pct=92.0, span_pct=8.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    final = probe(output)
    print(f"  本編: 保持区間 {len(keeps)}個 (合計 {main_len:.1f}s) / "
          f"intro {intro}s + outro {outro}s / dissolve {dissolve}s / "
          f"素材={asset_type} / 統合分割方式")
    print(f"  完了: {output} (長さ {float(final['format']['duration']):.1f}s, "
          f"想定 {expected:.1f}s)")


# ---------------------------------------------------------------------------
# 素材 (OP/ED) の解決
# ---------------------------------------------------------------------------
def _resolve_asset(thumb: Path, cfg: dict):
    """(asset_type, probe結果 or None) を返す。asset_type 未指定なら自動判定。"""
    asset_type = cfg.get("asset_type")
    info = None
    if asset_type is None:
        try:
            info = probe(thumb)
            asset_type = "video"
        except Exception:  # noqa: BLE001
            asset_type = "image"
    elif asset_type == "video":
        info = probe(thumb)
    return asset_type, info


def _asset_timing(asset_type, asset_info, cfg):
    """(intro秒, outro秒, 素材に音声があるか) を返す。
    動画素材はそのままの長さで頭とお尻に付ける。"""
    if asset_type == "video":
        dur = float(asset_info["format"]["duration"])
        has_audio = any(s["codec_type"] == "audio" for s in asset_info["streams"])
        return dur, dur, has_audio
    return float(cfg.get("intro_sec", 3)), float(cfg.get("outro_sec", 3)), False


# ---------------------------------------------------------------------------
# plan: カット案の計算 (書き出しはしない)
# ---------------------------------------------------------------------------
def compute_plan(input_video: Path, cfg: dict, transcript_path=None, thumb=None,
                 llm_client=None) -> dict:
    """動画 (+任意でZoom VTT) と設定からカット案 dict を作る。

    含まれるもの: 開始点 (trim_start) と終了点、カット後の保持区間、
    話者・登壇者推定 (VTTがある場合)、素材情報、想定の出力尺。

    カット候補は candidates に集められ、有効なものだけが保持区間から
    差し引かれる。候補の出所 (source) は:
      silence: 無音検出 / ops・other: デフォルト保持ポリシー (登壇者以外)
      llm: LLM検出 (llm_detect) / user: 人間が付けたカット
    llm_client が渡されれば LLM検出も行い、結果 (ok/skipped/failed) は
    plan["llm_detection"] に記録する。失敗しても plan は作り続ける。
    """
    input_video = Path(input_video)
    info = probe(input_video)
    vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    width = int(vstream["width"])
    height = int(vstream["height"])
    num, _, den = str(vstream.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1)
    if fps <= 0 or fps > 120:
        fps = 30.0
    total = float(info["format"]["duration"])
    _progress(f"動画情報を取得しました（{_mmss(total)}）")

    # --- 話者・登壇者推定 (Zoom文字起こしがある場合) ---
    speakers = None
    cues = []
    if transcript_path:
        _progress("文字起こしを解析しています…")
        sys.path.insert(0, str(Path(__file__).parent))
        from transcript import build_result, parse_transcript
        cues = parse_transcript(Path(transcript_path))
        speakers = build_result(cues)
        _progress(f"文字起こしを解析しました（キュー {len(cues)} 件、"
                  f"話者 {len(speakers.get('names') or {})} 人）")

    # --- 登壇者 (複数可) ---
    # cfg["presenters"] で話者インデックスを複数指定できる。未指定なら
    # 登壇者推定 (紹介パターン優先、無ければ最長話者) の1人を入れる。
    if cfg.get("presenters") is not None:
        presenters = [int(p) for p in cfg["presenters"]]
    elif speakers:
        guess = speakers.get("presenter_guess",
                             speakers.get("main_speaker_guess"))
        presenters = [int(guess)] if guess is not None else []
    else:
        presenters = []

    # --- 開始点・終了点 ---
    # 開始点は「指定された登壇者のうち最初に話し始めた人」の位置が基本。
    if cfg.get("trim_start") is not None:
        trim_start = parse_time(cfg["trim_start"])
        trim_source = "config"
    elif speakers and presenters:
        sug = _suggest_start(speakers, presenters)
        if sug is not None:
            trim_start = sug
            trim_source = "transcript"
        elif speakers.get("suggested_trim_start") is not None:
            trim_start = float(speakers["suggested_trim_start"])
            trim_source = "transcript"
        else:
            trim_start = 0.0
            trim_source = "default"
    elif speakers and speakers.get("suggested_trim_start") is not None:
        trim_start = float(speakers["suggested_trim_start"])
        trim_source = "transcript"
    else:
        trim_start = 0.0
        trim_source = "default"
    if cfg.get("end") is not None:
        end = parse_time(cfg["end"])
    else:
        end = total - parse_time(cfg.get("trim_tail", 0))
    if end - trim_start <= 0:
        raise ValueError(
            f"本編が0秒以下です (total={total:.1f}s, start={trim_start}, end={end})")
    _progress(f"開始点 {_mmss(trim_start)} / 終了点 {_mmss(end)} を決めました")

    # --- カット候補 (無音 / 保持ポリシー / LLM検出 / ユーザー) ---
    # 検出された区間はすべて candidates として plan に残し、
    # cfg["disabled_cuts"] ([[start, end], ...]) に入った区間は
    # 「切らない (残す)」として候補を無効化する。人間がエディタで
    # 区間ごとに残す/切るを切り替えるための仕組み。
    # cfg["user_cuts"] / cfg["llm_cuts"] ([[start, end], ...]) は、
    # エディタ側で確定した明示のカット区間 (人間が付けたカット /
    # 人間が有効のままにしたLLM候補) で、keeps から差し引かれる。
    min_sec = float(cfg.get("silence_sec", 3.0))
    db = int(cfg.get("silence_db", -38))
    sil_enabled = bool(cfg.get("silence_cut", False)) and has_audio
    disabled = cfg.get("disabled_cuts") or []

    def _is_disabled(c):
        # クリップで開始/終了がずれた区間とも一致するよう、
        # 区間長の半分以上が重なっていれば「残す」指定とみなす
        for d in disabled:
            overlap = min(c[1], float(d[1])) - max(c[0], float(d[0]))
            if overlap >= 0.5 * (c[1] - c[0]):
                return True
        return False

    candidates = []
    cuts = []

    def _add_candidate(cand):
        cand = dict(cand)
        cand["enabled"] = not _is_disabled((cand["start"], cand["end"]))
        candidates.append(cand)
        if cand["enabled"]:
            cuts.append((cand["start"], cand["end"]))

    # 無音カット
    if sil_enabled:
        _progress("無音を検出しています…")
        _all_keeps, cuts_all = _keep_segments(
            detect_silences(input_video, min_sec, db), trim_start, end, min_sec)
        for c in cuts_all:
            _add_candidate({"start": round(c[0], 3), "end": round(c[1], 3),
                            "kind": "silence", "source": "silence"})
        _progress(f"無音検出が終わりました（カット候補 {len(cuts_all)} 件）")

    # デフォルト保持ポリシー: 登壇者以外の発話を最初からカット候補にする。
    # cfg["keep_presenters_only"] = False で無効化できる。
    keep_presenters_only = bool(cfg.get("keep_presenters_only", True))
    policy_applied = False
    if keep_presenters_only and speakers and presenters:
        n_policy = 0
        for cand in _policy_candidates(speakers, cues, presenters,
                                       trim_start, end):
            _add_candidate(cand)
            policy_applied = True
            n_policy += 1
        if n_policy:
            _progress(f"登壇者以外の発話をカット候補にしました（{n_policy} 件）")

    # LLM検出 (transcript のもたつき・トラブル・本題外)
    llm_status = {"status": "skipped", "detail": "LLMクライアント未指定"}
    if llm_client is not None:
        if cues:
            try:
                sys.path.insert(0, str(Path(__file__).parent))
                import llm_detect
                _progress("AI検出を行います…")
                llm_stats = {}
                found = llm_detect.detect_ranges(cues, llm_client,
                                                 stats=llm_stats)
                for cand in found:
                    cs = max(cand["start"], trim_start)
                    ce = min(cand["end"], end)
                    if ce - cs > 0.05:
                        _add_candidate({**cand, "start": round(cs, 3),
                                        "end": round(ce, 3)})
                llm_status = {"status": "ok", "count": len(found),
                              "client": getattr(llm_client, "name", "unknown")}
                if llm_stats.get("partial"):
                    # 一部の窓だけ失敗した場合は、その旨を detail に残す
                    llm_status["detail"] = (
                        f"{llm_stats['windows_total']}窓中"
                        f"{llm_stats['windows_ok']}窓成功"
                        f"（{llm_stats['windows_failed']}窓は失敗したため未検出）")
                    _progress(f"AI検出が終わりました（候補 {len(found)} 件、"
                              f"{llm_status['detail']}）")
                else:
                    _progress(f"AI検出が終わりました（候補 {len(found)} 件）")
            except Exception as e:  # noqa: BLE001 - 検出失敗でも plan は作る
                _progress(f"AI検出は失敗しました（plan はそのまま続けます）: "
                          f"{str(e)[:120]}")
                llm_status = {"status": "failed", "detail": str(e)[:200]}
        else:
            llm_status = {"status": "skipped", "detail": "transcript がありません"}

    # 明示カット (ユーザー / LLM): [trim_start, end] に収まるものだけ有効
    user_cuts = []
    for u in cfg.get("user_cuts") or []:
        us, ue = max(float(u[0]), trim_start), min(float(u[1]), end)
        if ue - us > 0.05:
            user_cuts.append((us, ue))
            candidates.append({"start": round(us, 3), "end": round(ue, 3),
                               "kind": "user", "source": "user",
                               "enabled": True})
            cuts.append((us, ue))
    llm_cuts = []
    for u in cfg.get("llm_cuts") or []:
        us, ue = max(float(u[0]), trim_start), min(float(u[1]), end)
        if ue - us > 0.05:
            llm_cuts.append((us, ue))
            candidates.append({"start": round(us, 3), "end": round(ue, 3),
                               "kind": "llm", "source": "llm",
                               "enabled": True})
            cuts.append((us, ue))
    candidates.sort(key=lambda c: c["start"])
    cuts = sorted(cuts)
    keeps = []
    cur = trim_start
    for cs, ce in cuts:
        if cs > cur:
            keeps.append((cur, cs))
        cur = max(cur, ce)
    if cur < end:
        keeps.append((cur, end))
    if not keeps:
        raise ValueError("カット後に残る区間がありません")

    # --- 素材情報 ---
    asset = None
    if thumb is not None:
        thumb = Path(thumb)
        asset_type, asset_info = _resolve_asset(thumb, cfg)
        asset = {"path": str(thumb.resolve()), "type": asset_type}
        if asset_info is not None:
            asset["duration"] = float(asset_info["format"]["duration"])
            asset["has_audio"] = any(
                s["codec_type"] == "audio" for s in asset_info["streams"])

    # --- 想定の出力尺 ---
    dissolve = float(cfg.get("dissolve", 0.8))
    if asset and asset["type"] == "video":
        intro = outro = asset["duration"]
    else:
        intro = float(cfg.get("intro_sec", 3))
        outro = float(cfg.get("outro_sec", 3))
    _, main_len = _join_plan(keeps, dissolve, fps)
    expected = intro + main_len + outro - 2 * dissolve

    _progress(f"カット案ができました（保持区間 {len(keeps)} 個、"
              f"想定の出力尺 {_mmss(expected)}）")
    return {
        "version": 1,
        "video": {
            "path": str(input_video.resolve()),
            "duration": total,
            "width": width,
            "height": height,
            "fps": fps,
            "has_audio": has_audio,
        },
        "trim_start": trim_start,
        "end": end,
        "trim_start_source": trim_source,
        "speakers": speakers,
        "presenters": presenters,
        "keep_policy": {
            "keep_presenters_only": keep_presenters_only,
            "applied": policy_applied,
        },
        "llm_detection": llm_status,
        "silence": {
            "enabled": sil_enabled,
            "min_sec": min_sec,
            "db": db,
            "cuts": [[round(s, 3), round(e, 3)] for s, e in cuts],
            "user_cuts": [[round(s, 3), round(e, 3)] for s, e in user_cuts],
            "llm_cuts": [[round(s, 3), round(e, 3)] for s, e in llm_cuts],
            "candidates": candidates,
            "keeps": [[round(s, 3), round(e, 3)] for s, e in keeps],
        },
        "asset": asset,
        "render": {
            "dissolve": dissolve,
            "intro_sec": float(cfg.get("intro_sec", 3)),
            "outro_sec": float(cfg.get("outro_sec", 3)),
            "asset_type": cfg.get("asset_type"),
        },
        "expected_duration": round(expected, 2),
    }


# ---------------------------------------------------------------------------
# render: カット案からの書き出し
# ---------------------------------------------------------------------------
def _render_one_pass(plan, thumb, output, cfg, enc_args, progress_cb,
                     hwaccel=False):
    """保持区間のカット + OP/ED接続 + ディゾルブを単一フィルタグラフで行い、
    エンコード1回で書き出す。"""
    vinfo = plan["video"]
    video = Path(vinfo["path"])
    width, height, fps = vinfo["width"], vinfo["height"], vinfo["fps"]
    has_audio = vinfo["has_audio"]
    keeps = [tuple(k) for k in plan["silence"]["keeps"]]
    dissolve = float(cfg.get("dissolve", 0.8))

    asset_type, asset_info = _resolve_asset(thumb, cfg)
    intro, outro, asset_has_audio = _asset_timing(asset_type, asset_info, cfg)

    joins, main_len = _join_plan(keeps, dissolve, fps)
    if dissolve >= intro or dissolve >= outro or dissolve >= main_len:
        raise ValueError("ディゾルブ秒数がクリップ長より長いです。dissolveを短くしてください")

    offset1 = intro - dissolve
    offset2 = intro + main_len - 2 * dissolve
    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"

    filters = []
    # --- 映像: OP / 本編 (保持区間をつなぐ) / ED ---
    # xfade は入力同士のタイムベース一致を要求する。素材側は fps フィルタで
    # 1/fps になる一方、本編 concat は保持区間が1個のとき 1/1000000 になる
    # ことがあり不一致で失敗するため、xfade の全入力を settb=AVTB で統一する。
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS,settb=AVTB[v0]")
    for i, (a, b) in enumerate(keeps):
        filters.append(f"[1:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,{scale},settb=AVTB[vk{i}]")
    if joins:
        cur = "vk0"
        for k in range(1, len(keeps)):
            d, off = joins[k - 1]
            filters.append(f"[{cur}][vk{k}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            filters.append(f"[vx{k}]settb=AVTB[vxs{k}]")
            cur = f"vxs{k}"
        filters.append(f"[{cur}]settb=AVTB[vmain]")
    else:
        labels = "".join(f"[vk{i}]" for i in range(len(keeps)))
        filters.append(labels + f"concat=n={len(keeps)}:v=1:a=0,settb=AVTB[vmain]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS,settb=AVTB[v2]")
    filters.append(f"[v0][vmain]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx]settb=AVTB[vxs]")
    filters.append(f"[vxs][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

    # --- 音声 ---
    if asset_has_audio:
        filters.append(f"[0:a]atrim=duration={intro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a0]")
        filters.append(f"[2:a]atrim=duration={outro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a2]")
    else:
        filters.append("[3:a]asplit=2[sil1][sil2]")
        filters.append(f"[sil1]atrim=duration={intro},asetpts=PTS-STARTPTS[a0]")
        filters.append(f"[sil2]atrim=duration={outro},asetpts=PTS-STARTPTS[a2]")
    if has_audio:
        for i, (a, b) in enumerate(keeps):
            filters.append(f"[1:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[ak{i}]")
        if joins:
            cur = "ak0"
            for k in range(1, len(keeps)):
                d, _off = joins[k - 1]
                filters.append(f"[{cur}][ak{k}]acrossfade=d={d:.3f}[ax{k}]")
                cur = f"ax{k}"
            filters.append(f"[{cur}]anull[amain]")
        else:
            labels = "".join(f"[ak{i}]" for i in range(len(keeps)))
            filters.append(labels + f"concat=n={len(keeps)}:v=0:a=1[amain]")
    else:
        filters.append(f"anullsrc=cl=stereo:r=48000,atrim=duration={main_len},asetpts=PTS-STARTPTS[amain]")
    filters.append(f"[a0][amain]acrossfade=d={dissolve}[ax]")
    filters.append(f"[ax][a2]acrossfade=d={dissolve}[aout]")

    filter_complex = ";".join(filters)
    silent_dur = intro + main_len + outro

    if asset_type == "video":
        asset_in = ["-i", str(thumb)]
        asset_in_outro = asset_in
    else:
        asset_in = ["-loop", "1", "-framerate", str(fps), "-t", str(intro), "-i", str(thumb)]
        asset_in_outro = ["-loop", "1", "-framerate", str(fps), "-t", str(outro), "-i", str(thumb)]
    cmd = ["ffmpeg", "-y"]
    cmd += asset_in
    if hwaccel:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(video)]
    cmd += asset_in_outro
    cmd += [
        "-f", "lavfi", "-t", str(silent_dur), "-i", "anullsrc=cl=stereo:r=48000",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        *enc_args, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    print("実行:", " ".join(cmd[:6]), "...")
    print(f"  本編: 保持区間 {len(keeps)}個 (合計 {main_len:.1f}s) / "
          f"intro {intro}s + outro {outro}s / dissolve {dissolve}s / 素材={asset_type} / 1パス")
    expected = intro + main_len + outro - 2 * dissolve
    _run_ffmpeg(cmd, expected, progress_cb, base_pct=0, span_pct=100)
    final = probe(output)
    print(f"  完了: {output} (長さ {float(final['format']['duration']):.1f}s, "
          f"想定 {expected:.1f}s)")


def _final_pass(main_input, main_start, main_end, thumb, asset_type, asset_info,
                has_audio, width, height, fps, cfg, enc_args, output,
                progress_cb, base_pct, span_pct, hwaccel=False):
    """OP/ED接続の最終パス (従来方式)。main_input は本編ファイル (中間ファイル可)。"""
    dissolve = float(cfg.get("dissolve", 0.8))
    intro, outro, asset_has_audio = _asset_timing(asset_type, asset_info, cfg)
    main_dur2 = main_end - main_start

    if dissolve >= intro or dissolve >= outro or dissolve >= main_dur2:
        raise ValueError("ディゾルブ秒数がクリップ長より長いです。dissolveを短くしてください")

    offset1 = intro - dissolve
    offset2 = intro + main_dur2 - 2 * dissolve
    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"

    filters = []
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS,settb=AVTB[v0]")
    filters.append(f"[1:v]trim=start={main_start}:end={main_end},setpts=PTS-STARTPTS,{scale},settb=AVTB[v1]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS,settb=AVTB[v2]")
    filters.append(f"[v0][v1]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx]settb=AVTB[vxs]")
    filters.append(f"[vxs][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

    if asset_has_audio:
        filters.append(f"[0:a]atrim=duration={intro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a0]")
        filters.append(f"[2:a]atrim=duration={outro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a2]")
    else:
        filters.append("[3:a]asplit=2[sil1][sil2]")
        filters.append(f"[sil1]atrim=duration={intro},asetpts=PTS-STARTPTS[a0]")
        filters.append(f"[sil2]atrim=duration={outro},asetpts=PTS-STARTPTS[a2]")
    if has_audio:
        filters.append(f"[1:a]atrim=start={main_start}:end={main_end},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a1]")
    else:
        filters.append(f"anullsrc=cl=stereo:r=48000,atrim=duration={main_dur2},asetpts=PTS-STARTPTS[a1]")
    filters.append(f"[a0][a1]acrossfade=d={dissolve}[ax]")
    filters.append(f"[ax][a2]acrossfade=d={dissolve}[aout]")

    filter_complex = ";".join(filters)
    silent_dur = intro + main_dur2 + outro

    if asset_type == "video":
        asset_in = ["-i", str(thumb)]
        asset_in_outro = asset_in
    else:
        asset_in = ["-loop", "1", "-framerate", str(fps), "-t", str(intro), "-i", str(thumb)]
        asset_in_outro = ["-loop", "1", "-framerate", str(fps), "-t", str(outro), "-i", str(thumb)]
    cmd = ["ffmpeg", "-y"]
    cmd += asset_in
    if hwaccel:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(main_input)]
    cmd += asset_in_outro
    cmd += [
        "-f", "lavfi", "-t", str(silent_dur), "-i", "anullsrc=cl=stereo:r=48000",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        *enc_args, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    print("実行:", " ".join(cmd[:6]), "...")
    print(f"  本編: {main_start:.1f}s 〜 {main_end:.1f}s (残り {main_dur2:.1f}s) / "
          f"intro {intro}s + outro {outro}s / dissolve {dissolve}s / 素材={asset_type}")
    expected = intro + main_dur2 + outro - 2 * dissolve
    _run_ffmpeg(cmd, expected, progress_cb, base_pct=base_pct, span_pct=span_pct)
    final = probe(output)
    print(f"  完了: {output} (長さ {float(final['format']['duration']):.1f}s, "
          f"想定 {expected:.1f}s)")


def _render_legacy(plan, thumb, output, cfg, enc_args, progress_cb,
                   workdir=None, hwaccel=False):
    """従来方式: 無音カット時は中間ファイルを作ってから最終パス (2パス)。
    中間ファイル (本編カット) は workdir があればそこに作る。"""
    vinfo = plan["video"]
    video = Path(vinfo["path"])
    width, height, fps = vinfo["width"], vinfo["height"], vinfo["fps"]
    has_audio = vinfo["has_audio"]
    trim_start, end = float(plan["trim_start"]), float(plan["end"])
    main_dur = end - trim_start
    dissolve = float(cfg.get("dissolve", 0.8))
    crf = int(cfg.get("crf", 18))
    preset = str(cfg.get("preset", "fast"))
    asset_type, asset_info = _resolve_asset(thumb, cfg)

    keeps = [tuple(k) for k in plan["silence"]["keeps"]]
    single = (len(keeps) == 1 and abs(keeps[0][0] - trim_start) < 0.01
              and abs(keeps[0][1] - end) < 0.01)

    main_input = video
    main_start, main_end = trim_start, end
    base_pct, span_pct = 0, 100
    tmp_cut = None
    # 保持区間が範囲全体と異なる場合は、その出所 (無音 / 保持ポリシー /
    # LLM / ユーザー) によらず本編カットを作る。以前は無音カット有効時の
    # みだったため、他方式のカットがフォールバック時に落ちていた。
    if not single:
        if workdir is not None:
            tmp_cut = Path(workdir) / (output.stem + "_maincut.mp4")
        else:
            tmp_cut = output.with_name(output.stem + "_maincut.mp4")
        _build_main_cut(video, keeps, width, height, fps, tmp_cut,
                        max(10, crf - 3), preset, progress_cb, main_dur,
                        dissolve=dissolve, workdir=workdir, hwaccel=hwaccel)
        main_input = tmp_cut
        main_start = 0.0
        main_end = float(probe(tmp_cut)["format"]["duration"])
        base_pct, span_pct = 30, 70
    try:
        _final_pass(main_input, main_start, main_end, thumb, asset_type, asset_info,
                    has_audio, width, height, fps, cfg, enc_args, output,
                    progress_cb, base_pct, span_pct, hwaccel=hwaccel)
    finally:
        if tmp_cut:
            tmp_cut.unlink(missing_ok=True)


def _resolve_workdir(cfg, output=None, need_bytes=0) -> Path:
    """作業ディレクトリを決める: cfg "workdir" > 環境変数 EDIT_WORKDIR
    > システムの一時領域。存在しなければ作る。

    既定の一時領域が小さな tmpfs 等で、必要量 (need_bytes) を置く空きが
    無い場合は、出力先の親ディレクトリにフォールバックする (断片は
    元動画と同程度の容量になるため、置き場所の空き不足は致命的)。
    明示指定 (cfg / 環境変数) がある場合はそのまま使う。"""
    raw = cfg.get("workdir") or os.environ.get("EDIT_WORKDIR")
    if raw:
        wd = Path(raw)
        wd.mkdir(parents=True, exist_ok=True)
        return wd
    tmp = Path(tempfile.gettempdir())

    def _free(p):
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return 0

    if _free(tmp) >= need_bytes:
        return tmp
    if output is not None:
        parent = Path(output).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if _free(parent) > _free(tmp):
            _progress("一時領域の空きが不足するため、作業ファイルを出力先の"
                      "フォルダに置きます")
            return parent
    return tmp


def _is_other_fs(path, ref_dir) -> bool:
    """path が ref_dir と別ファイルシステムにあるか (st_dev 比較)。
    判定できない場合は安全側 (別FS扱い = コピーする) に倒す。"""
    try:
        return os.stat(path).st_dev != os.stat(ref_dir).st_dev
    except OSError:
        return True


def _localize_input(path, job_tmp, tag) -> Path:
    """入力が作業領域と別FS (Google Drive の FUSE 等) にあれば job_tmp へ
    1回だけコピーしてローカルパスを返す。同一FSならそのまま返す。
    断片レンダーごとに低速なリモートファイルへ繰り返しアクセスするのを
    避けるための寄せ処理。"""
    path = Path(path)
    if not _is_other_fs(path, job_tmp):
        return path
    dest = job_tmp / f"{tag}{path.suffix}"
    _progress(f"入力を作業領域にコピーしています: {path.name}")
    shutil.copy2(path, dest)
    return dest


def render_plan(plan: dict, output: Path, cfg: dict = None, thumb=None, progress_cb=None):
    """カット案 dict から最終書き出しを行う。

    cfg で上書きできるのは表示・品質系 (dissolve, intro_sec, outro_sec,
    asset_type, crf, preset, encoder, one_pass, workdir, hwaccel) のみ。
    開始点・保持区間は plan 側が正。

    中間ファイルは作業ディレクトリ (cfg "workdir" / 環境変数
    EDIT_WORKDIR / 既定は一時領域) の下に作り、終わったら消す。
    入力が別FSにある場合は作業ディレクトリへ寄せてから処理する。
    """
    cfg = dict(cfg or {})
    eff = dict(plan.get("render") or {})
    for key in ("dissolve", "intro_sec", "outro_sec", "asset_type",
                "crf", "preset", "encoder", "nvenc_cq", "nvenc_preset",
                "one_pass", "workdir", "hwaccel"):
        if cfg.get(key) is not None:
            eff[key] = cfg[key]
    if thumb is None and plan.get("asset"):
        thumb = plan["asset"]["path"]
    if thumb is None:
        raise ValueError("OP/ED素材 (thumb) が指定されていません")
    thumb = Path(thumb)
    output = Path(output)

    enc_args, enc_name = _encoder_args(eff)
    print(f"  エンコーダ: {enc_name}")
    hwaccel = (str(eff.get("hwaccel", "")).lower() == "cuda"
               and enc_name == "h264_nvenc")
    if eff.get("hwaccel") and not hwaccel:
        print("  hwaccel: NVENC 使用時のみ有効なため今回は使いません")

    # 断片は元動画と同程度の容量になる。作業領域の空き判定に使う
    need_bytes = 0
    for p in (plan["video"]["path"], thumb):
        try:
            need_bytes += os.path.getsize(p)
        except OSError:
            pass
    need_bytes = max(512 * 1024 * 1024, int(need_bytes * 1.5))
    workdir = _resolve_workdir(eff, output=output, need_bytes=need_bytes)
    job_tmp = Path(tempfile.mkdtemp(prefix="edit_render_", dir=str(workdir)))
    try:
        local_video = _localize_input(plan["video"]["path"], job_tmp,
                                      "input_video")
        local_thumb = _localize_input(thumb, job_tmp, "input_asset")
        plan2 = dict(plan)
        plan2["video"] = dict(plan["video"])
        plan2["video"]["path"] = str(local_video)

        if eff.get("one_pass", True):
            keeps = [tuple(k) for k
                     in (plan2.get("silence") or {}).get("keeps") or []]
            if keeps and _needs_smart_render(keeps):
                # 1パス方式は全区間を1グラフでつなぐため、区間が多いと
                # メモリを使い切って ffmpeg が kill される。メモリ安全な
                # 統合分割方式 (OP/ED込みの断片計画 + concat) を使う。
                _progress("保持区間が多いため、メモリ安全な統合分割方式で書き出します")
                try:
                    _build_unified(plan2, local_thumb, output, eff, enc_args,
                                   progress_cb, job_tmp, hwaccel=hwaccel)
                    return
                except RuntimeError as e:
                    print(f"  統合分割方式に失敗したため従来方式 (2パス) にフォールバックします: {e}")
            else:
                try:
                    _render_one_pass(plan2, local_thumb, output, eff, enc_args,
                                     progress_cb, hwaccel=hwaccel)
                    return
                except RuntimeError as e:
                    print(f"  1パス書き出しに失敗したため従来方式 (2パス) にフォールバックします: {e}")
        _render_legacy(plan2, local_thumb, output, eff, enc_args, progress_cb,
                       workdir=job_tmp, hwaccel=hwaccel)
    finally:
        shutil.rmtree(job_tmp, ignore_errors=True)


def build_and_run(input_video: Path, thumb: Path, output: Path, cfg: dict, progress_cb=None):
    """従来互換のエントリポイント: カット案を作ってそのまま書き出す。
    cfg に "transcript" (VTTパス) があれば開始点候補に使う。"""
    cfg = dict(cfg or {})
    plan = compute_plan(Path(input_video), cfg,
                        transcript_path=cfg.get("transcript"), thumb=Path(thumb),
                        llm_client=cfg.get("llm_client"))
    cuts = plan["silence"]["cuts"]
    if cuts:
        removed = sum(e - s for s, e in cuts)
        print(f"  カット: {len(cuts)}箇所 / 合計 {removed:.1f}秒を除去 (有効な候補すべて)")
    render_plan(plan, Path(output), cfg=cfg, thumb=Path(thumb), progress_cb=progress_cb)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_cfg(args) -> dict:
    cfg = {}
    if getattr(args, "config", None):
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    return cfg


def _overlay(cfg: dict, pairs):
    for key, val in pairs:
        if val is not None:
            cfg[key] = val
    return cfg


def _plan_cli(argv):
    ap = argparse.ArgumentParser(
        prog="edit.py plan", description="カット案 (plan JSON) を作る。書き出しはしない")
    ap.add_argument("--video", required=True, help="入力動画 mp4")
    ap.add_argument("--transcript", help="Zoom文字起こし VTT (開始点候補・話者推定に使う)")
    ap.add_argument("--thumb", help="OP/ED素材 (画像 or 動画)。素材情報を plan に含める")
    ap.add_argument("--out", help="plan JSON の出力先 (省略時は標準出力)")
    ap.add_argument("--config", help="設定ファイル (json)")
    ap.add_argument("--trim-start", type=float, help="頭からカットする秒数 (OP)")
    ap.add_argument("--trim-tail", type=float, help="末尾からカットする秒数 (ED)")
    ap.add_argument("--end", help="本編の終了位置を直接指定 (秒 or HH:MM:SS)")
    ap.add_argument("--intro-sec", type=float, help="頭サムネの表示秒数")
    ap.add_argument("--outro-sec", type=float, help="お尻サムネの表示秒数")
    ap.add_argument("--dissolve", type=float, help="ディゾルブ秒数")
    ap.add_argument("--asset-type", choices=["image", "video"], help="OP/ED素材の種類 (省略時は自動判定)")
    ap.add_argument("--silence-cut", action="store_true", default=None, help="指定秒数以上の無音をカットする")
    ap.add_argument("--silence-sec", type=float, help="無音カットの最小秒数 (既定3)")
    ap.add_argument("--silence-db", type=int, help="無音判定のしきい値 dB (既定-38)")
    ap.add_argument("--presenters", help="登壇者の話者インデックス (カンマ区切り、複数可。例: 0,1)")
    ap.add_argument("--no-keep-presenters-only", action="store_true",
                    help="デフォルト保持ポリシー (登壇者以外をカット候補にする) を無効化")
    ap.add_argument("--llm-detect", action="store_true",
                    help="LLM検出を行う (クライアントは環境変数から選択: llm_detect.get_client_from_env)")
    ap.add_argument("--mock-llm", action="store_true",
                    help="LLM検出にモッククライアントを使う (テスト・デモ用)")
    args = ap.parse_args(argv)

    cfg = _load_cfg(args)
    _overlay(cfg, [("trim_start", args.trim_start), ("trim_tail", args.trim_tail),
                   ("end", args.end), ("intro_sec", args.intro_sec),
                   ("outro_sec", args.outro_sec), ("dissolve", args.dissolve),
                   ("asset_type", args.asset_type), ("silence_sec", args.silence_sec),
                   ("silence_db", args.silence_db)])
    if args.presenters:
        cfg["presenters"] = [int(x) for x in args.presenters.split(",") if x.strip()]
    if args.silence_cut:
        cfg["silence_cut"] = True
    if args.no_keep_presenters_only:
        cfg["keep_presenters_only"] = False

    llm_client = None
    if args.mock_llm:
        sys.path.insert(0, str(Path(__file__).parent))
        import llm_detect
        llm_client = llm_detect.MockLLMClient()
    elif args.llm_detect:
        sys.path.insert(0, str(Path(__file__).parent))
        import llm_detect
        llm_client = llm_detect.get_client_from_env()

    plan = compute_plan(Path(args.video), cfg,
                        transcript_path=args.transcript,
                        thumb=Path(args.thumb) if args.thumb else None,
                        llm_client=llm_client)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        sp = plan.get("speakers") or {}
        print(f"plan を書きました: {args.out}")
        print(f"  開始点: {plan['trim_start']}s (出所: {plan['trim_start_source']}) / "
              f"終了: {plan['end']}s / 想定尺: {plan['expected_duration']}s")
        if sp:
            print(f"  登壇者推定: {sp.get('main_speaker_guess')} "
                  f"(名前: {sp.get('names')}) / 話者別秒数: {sp.get('speakers')}")
        if plan["silence"]["cuts"]:
            print(f"  カット: {len(plan['silence']['cuts'])}箇所 (有効な候補すべて)")
        by_src = {}
        for c in plan["silence"]["candidates"]:
            if c.get("enabled"):
                by_src[c.get("source", "?")] = by_src.get(c.get("source", "?"), 0) + 1
        if by_src:
            print(f"  候補の内訳 (有効): {by_src}")
        print(f"  LLM検出: {plan['llm_detection']}")
    else:
        print(text)


def _render_cli(argv):
    ap = argparse.ArgumentParser(
        prog="edit.py render", description="カット案 (plan JSON) から書き出す")
    ap.add_argument("--plan", required=True, help="plan JSON のパス")
    ap.add_argument("--thumb", help="OP/ED素材 (省略時は plan に記録された素材)")
    ap.add_argument("-o", "--output", help="出力ファイル (省略時は 元動画名_edited.mp4)")
    ap.add_argument("--config", help="設定ファイル (json)。表示・品質系のみ上書き可")
    ap.add_argument("--dissolve", type=float, help="ディゾルブ秒数")
    ap.add_argument("--intro-sec", type=float, help="頭サムネの表示秒数")
    ap.add_argument("--outro-sec", type=float, help="お尻サムネの表示秒数")
    ap.add_argument("--asset-type", choices=["image", "video"], help="OP/ED素材の種類")
    ap.add_argument("--encoder", choices=["auto", "x264", "nvenc"], help="エンコーダ (既定auto)")
    ap.add_argument("--crf", type=int, help="libx264 の crf")
    ap.add_argument("--preset", help="libx264 の preset")
    ap.add_argument("--workdir", help="中間ファイル用の作業ディレクトリ (既定: 環境変数 EDIT_WORKDIR か一時領域)")
    ap.add_argument("--hwaccel", action="store_true", help="NVENC使用時に入力デコードへ -hwaccel cuda を付ける")
    ap.add_argument("--two-pass", action="store_true", help="従来の2パス方式で書き出す")
    args = ap.parse_args(argv)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    cfg = _load_cfg(args)
    _overlay(cfg, [("dissolve", args.dissolve), ("intro_sec", args.intro_sec),
                   ("outro_sec", args.outro_sec), ("asset_type", args.asset_type),
                   ("encoder", args.encoder), ("crf", args.crf),
                   ("preset", args.preset), ("workdir", args.workdir)])
    if args.hwaccel:
        cfg["hwaccel"] = "cuda"
    if args.two_pass:
        cfg["one_pass"] = False
    if args.output:
        output = Path(args.output)
    else:
        v = Path(plan["video"]["path"])
        output = v.with_name(v.stem + "_edited.mp4")

    _pct_state = {"last": -10}

    def _render_progress(pct):
        # エンコード中も無言にならないよう、10% 刻みで進捗を出す
        if pct >= 100 or pct - _pct_state["last"] >= 10:
            _pct_state["last"] = pct
            print(f"  書き出し中… {pct}%", flush=True)

    render_plan(plan, output, cfg=cfg,
                thumb=Path(args.thumb) if args.thumb else None,
                progress_cb=_render_progress)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "plan":
        _plan_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "render":
        _render_cli(sys.argv[2:])
        return

    ap = argparse.ArgumentParser(description="Zoom動画 自動編集 (OP/EDカット + サムネ挿入 + ディゾルブ)")
    ap.add_argument("input", nargs="?", help="入力動画 mp4")
    ap.add_argument("thumb", nargs="?", help="Canvaサムネイル画像 png/jpg")
    ap.add_argument("-o", "--output", help="出力ファイル")
    ap.add_argument("--config", help="設定ファイル (json)")
    ap.add_argument("--batch", action="store_true", help="input/ フォルダ内をまとめて処理")
    ap.add_argument("--trim-start", type=float, help="頭からカットする秒数 (OP)")
    ap.add_argument("--trim-tail", type=float, help="末尾からカットする秒数 (ED)")
    ap.add_argument("--end", help="本編の終了位置を直接指定 (秒 or HH:MM:SS)")
    ap.add_argument("--intro-sec", type=float, help="頭サムネの表示秒数")
    ap.add_argument("--outro-sec", type=float, help="お尻サムネの表示秒数")
    ap.add_argument("--dissolve", type=float, help="ディゾルブ秒数")
    ap.add_argument("--asset-type", choices=["image", "video"], help="OP/ED素材の種類 (省略時は自動判定)")
    ap.add_argument("--silence-cut", action="store_true", help="指定秒数以上の無音をカットする")
    ap.add_argument("--silence-sec", type=float, help="無音カットの最小秒数 (既定3)")
    ap.add_argument("--encoder", choices=["auto", "x264", "nvenc"], help="エンコーダ (既定auto)")
    ap.add_argument("--two-pass", action="store_true", help="従来の2パス方式で書き出す")
    args = ap.parse_args()

    cfg = _load_cfg(args)
    # CLI指定があれば config より優先
    _overlay(cfg, [("trim_start", args.trim_start), ("trim_tail", args.trim_tail),
                   ("end", args.end), ("intro_sec", args.intro_sec),
                   ("outro_sec", args.outro_sec), ("dissolve", args.dissolve),
                   ("asset_type", args.asset_type), ("silence_sec", args.silence_sec),
                   ("encoder", args.encoder)])
    if args.silence_cut:
        cfg["silence_cut"] = True
    if args.two_pass:
        cfg["one_pass"] = False

    base = Path(__file__).parent
    if args.batch:
        in_dir = Path(cfg.get("input_dir", base / "input"))
        out_dir = Path(cfg.get("output_dir", base / "output"))
        thumb = Path(cfg.get("thumb", in_dir / "thumb.png"))
        if not thumb.exists():
            sys.exit(f"サムネイルが見つかりません: {thumb}")
        videos = sorted(in_dir.glob("*.mp4"))
        if not videos:
            sys.exit(f"{in_dir} に mp4 がありません")
        out_dir.mkdir(parents=True, exist_ok=True)
        for v in videos:
            out = out_dir / f"{v.stem}_edited.mp4"
            print(f"\n=== {v.name} ===")
            build_and_run(v, thumb, out, cfg)
        print(f"\nバッチ完了: {len(videos)}本 -> {out_dir}")
        return

    if not args.input or not args.thumb:
        ap.error("input と thumb を指定してください (または --batch)")
    output = Path(args.output) if args.output else Path(args.input).with_name(
        Path(args.input).stem + "_edited.mp4")
    build_and_run(Path(args.input), Path(args.thumb), output, cfg)


if __name__ == "__main__":
    main()
