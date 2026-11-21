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
  エンコード)。失敗した場合は従来の中間ファイル方式 (2パス) に自動で
  フォールバックする。cfg "one_pass": false で従来方式を強制できる。
"""
import argparse
import json
import re
import subprocess
import sys
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


def _run_ffmpeg(cmd, expected, progress_cb=None, base_pct=0, span_pct=100):
    """ffmpeg を進捗コールバック付きで実行する"""
    cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    err_lines = []

    def drain_err():
        for ln in proc.stderr:
            err_lines.append(ln)
            if len(err_lines) > 80:
                del err_lines[:40]

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
        tail = "".join(err_lines)[-1500:]
        print(tail, file=sys.stderr)
        raise RuntimeError(f"ffmpeg が失敗しました: {tail.strip()[-300:]}")


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


def _build_main_cut(input_video, keeps, width, height, fps, out_path,
                    crf, preset, progress_cb, main_dur, dissolve=0.0):
    """無音を除いた本編だけの中間ファイルを作る (従来方式・フォールバック用)。
    つなぎ目にはディゾルブ (xfade/acrossfade) をかける。"""
    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"
    filters = []
    for i, (a, b) in enumerate(keeps):
        filters.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,{scale}[v{i}]")
        filters.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    joins, _ = _join_plan(keeps, dissolve, fps)
    if not joins:
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
        filters.append(labels + f"concat=n={len(keeps)}:v=1:a=1[v][a]")
    else:
        cur_v, cur_a = "v0", "a0"
        for k in range(1, len(keeps)):
            d, off = joins[k - 1]
            filters.append(f"[{cur_v}][v{k}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            filters.append(f"[{cur_a}][a{k}]acrossfade=d={d:.3f}[ax{k}]")
            cur_v, cur_a = f"vx{k}", f"ax{k}"
        filters.append(f"[{cur_v}]null[v]")
        filters.append(f"[{cur_a}]anull[a]")
    cmd = ["ffmpeg", "-y", "-i", str(input_video),
           "-filter_complex", ";".join(filters),
           "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", str(out_path)]
    _run_ffmpeg(cmd, main_dur, progress_cb, base_pct=0, span_pct=30)


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
def compute_plan(input_video: Path, cfg: dict, transcript_path=None, thumb=None) -> dict:
    """動画 (+任意でZoom VTT) と設定からカット案 dict を作る。

    含まれるもの: 開始点 (trim_start) と終了点、無音カット後の保持区間、
    話者・登壇者推定 (VTTがある場合)、素材情報、想定の出力尺。
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

    # --- 話者・登壇者推定 (Zoom文字起こしがある場合) ---
    speakers = None
    if transcript_path:
        sys.path.insert(0, str(Path(__file__).parent))
        from transcript import result_from_transcript
        speakers = result_from_transcript(Path(transcript_path))

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

    # --- カット (無音カット + ユーザーカット) ---
    # 検出された無音はすべて candidates として plan に残し、
    # cfg["disabled_cuts"] ([[start, end], ...]) に入った区間は
    # 「切らない (残す)」として keeps 側に回す。人間がエディタで
    # 区間ごとに残す/切るを切り替えるための仕組み。
    # cfg["user_cuts"] ([[start, end], ...]) は、人間が保持区間から
    # 追加で切りたい区間。candidates には source: "user" で入り、
    # keeps から差し引かれる (無音カットが OFF でも有効)。
    min_sec = float(cfg.get("silence_sec", 3.0))
    db = int(cfg.get("silence_db", -38))
    sil_enabled = bool(cfg.get("silence_cut", False)) and has_audio
    candidates = []
    cuts = []
    if sil_enabled:
        _all_keeps, cuts_all = _keep_segments(
            detect_silences(input_video, min_sec, db), trim_start, end, min_sec)
        disabled = cfg.get("disabled_cuts") or []

        def _is_disabled(c):
            # クリップで開始/終了がずれた区間とも一致するよう、
            # 区間長の半分以上が重なっていれば「残す」指定とみなす
            for d in disabled:
                overlap = min(c[1], float(d[1])) - max(c[0], float(d[0]))
                if overlap >= 0.5 * (c[1] - c[0]):
                    return True
            return False

        for c in cuts_all:
            enabled = not _is_disabled(c)
            candidates.append({"start": round(c[0], 3), "end": round(c[1], 3),
                               "kind": "silence", "source": "silence",
                               "enabled": enabled})
            if enabled:
                cuts.append(c)
    # ユーザーカット: [trim_start, end] に収まるものだけ有効
    user_cuts = []
    for u in cfg.get("user_cuts") or []:
        us, ue = max(float(u[0]), trim_start), min(float(u[1]), end)
        if ue - us > 0.05:
            user_cuts.append((us, ue))
            candidates.append({"start": round(us, 3), "end": round(ue, 3),
                               "kind": "user", "source": "user",
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
        "silence": {
            "enabled": sil_enabled,
            "min_sec": min_sec,
            "db": db,
            "cuts": [[round(s, 3), round(e, 3)] for s, e in cuts],
            "user_cuts": [[round(s, 3), round(e, 3)] for s, e in user_cuts],
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
def _render_one_pass(plan, thumb, output, cfg, enc_args, progress_cb):
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
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS[v0]")
    for i, (a, b) in enumerate(keeps):
        filters.append(f"[1:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,{scale}[vk{i}]")
    if joins:
        cur = "vk0"
        for k in range(1, len(keeps)):
            d, off = joins[k - 1]
            filters.append(f"[{cur}][vk{k}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            cur = f"vx{k}"
        filters.append(f"[{cur}]null[vmain]")
    else:
        labels = "".join(f"[vk{i}]" for i in range(len(keeps)))
        filters.append(labels + f"concat=n={len(keeps)}:v=1:a=0[vmain]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS[v2]")
    filters.append(f"[v0][vmain]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

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
                progress_cb, base_pct, span_pct):
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
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS[v0]")
    filters.append(f"[1:v]trim=start={main_start}:end={main_end},setpts=PTS-STARTPTS,{scale}[v1]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS[v2]")
    filters.append(f"[v0][v1]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

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


def _render_legacy(plan, thumb, output, cfg, enc_args, progress_cb):
    """従来方式: 無音カット時は中間ファイルを作ってから最終パス (2パス)。"""
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
    if plan["silence"]["enabled"] and not single:
        tmp_cut = output.with_name(output.stem + "_maincut.mp4")
        _build_main_cut(video, keeps, width, height, fps, tmp_cut,
                        max(10, crf - 3), preset, progress_cb, main_dur,
                        dissolve=dissolve)
        main_input = tmp_cut
        main_start = 0.0
        main_end = float(probe(tmp_cut)["format"]["duration"])
        base_pct, span_pct = 30, 70
    try:
        _final_pass(main_input, main_start, main_end, thumb, asset_type, asset_info,
                    has_audio, width, height, fps, cfg, enc_args, output,
                    progress_cb, base_pct, span_pct)
    finally:
        if tmp_cut:
            tmp_cut.unlink(missing_ok=True)


def render_plan(plan: dict, output: Path, cfg: dict = None, thumb=None, progress_cb=None):
    """カット案 dict から最終書き出しを行う。

    cfg で上書きできるのは表示・品質系 (dissolve, intro_sec, outro_sec,
    asset_type, crf, preset, encoder, one_pass) のみ。開始点・保持区間は
    plan 側が正。
    """
    cfg = dict(cfg or {})
    eff = dict(plan.get("render") or {})
    for key in ("dissolve", "intro_sec", "outro_sec", "asset_type",
                "crf", "preset", "encoder", "nvenc_cq", "nvenc_preset", "one_pass"):
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
    if eff.get("one_pass", True):
        try:
            _render_one_pass(plan, thumb, output, eff, enc_args, progress_cb)
            return
        except RuntimeError as e:
            print(f"  1パス書き出しに失敗したため従来方式 (2パス) にフォールバックします: {e}")
    _render_legacy(plan, thumb, output, eff, enc_args, progress_cb)


def build_and_run(input_video: Path, thumb: Path, output: Path, cfg: dict, progress_cb=None):
    """従来互換のエントリポイント: カット案を作ってそのまま書き出す。
    cfg に "transcript" (VTTパス) があれば開始点候補に使う。"""
    cfg = dict(cfg or {})
    plan = compute_plan(Path(input_video), cfg,
                        transcript_path=cfg.get("transcript"), thumb=Path(thumb))
    cuts = plan["silence"]["cuts"]
    if cuts:
        removed = sum(e - s for s, e in cuts)
        print(f"  カット: {len(cuts)}箇所 / 合計 {removed:.1f}秒を除去 (無音+手動)")
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

    plan = compute_plan(Path(args.video), cfg,
                        transcript_path=args.transcript,
                        thumb=Path(args.thumb) if args.thumb else None)
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
            print(f"  無音カット: {len(plan['silence']['cuts'])}箇所")
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
    ap.add_argument("--two-pass", action="store_true", help="従来の2パス方式で書き出す")
    args = ap.parse_args(argv)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    cfg = _load_cfg(args)
    _overlay(cfg, [("dissolve", args.dissolve), ("intro_sec", args.intro_sec),
                   ("outro_sec", args.outro_sec), ("asset_type", args.asset_type),
                   ("encoder", args.encoder), ("crf", args.crf),
                   ("preset", args.preset)])
    if args.two_pass:
        cfg["one_pass"] = False
    if args.output:
        output = Path(args.output)
    else:
        v = Path(plan["video"]["path"])
        output = v.with_name(v.stem + "_edited.mp4")
    render_plan(plan, output, cfg=cfg,
                thumb=Path(args.thumb) if args.thumb else None)


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
