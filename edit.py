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

OP/EDの切り方:
  --trim-start : 頭から何秒カットするか
  --trim-tail  : 末尾から何秒カットするか
  --end        : こちらを直接指定も可 (秒 or HH:MM:SS)。--trim-tailより優先
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


def _build_main_cut(input_video, keeps, width, height, fps, out_path,
                    crf, preset, progress_cb, main_dur, dissolve=0.0):
    """無音を除いた本編だけの中間ファイルを作る。
    つなぎ目にはディゾルブ (xfade/acrossfade) をかける。
    短い区間ではディゾルブを自動で短くし、最低限の長さも確保する。"""
    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"
    filters = []
    for i, (a, b) in enumerate(keeps):
        filters.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,{scale}[v{i}]")
        filters.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    if len(keeps) == 1 or dissolve <= 0:
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
        filters.append(labels + f"concat=n={len(keeps)}:v=1:a=1[v][a]")
    else:
        # 区間をつないだ累積長を管理しながら xfade をチェーンする
        cur_v, cur_a = "v0", "a0"
        acc = keeps[0][1] - keeps[0][0]
        for k in range(1, len(keeps)):
            prev_len = keeps[k - 1][1] - keeps[k - 1][0]
            seg_len = keeps[k][1] - keeps[k][0]
            d = min(dissolve, 0.45 * prev_len, 0.45 * seg_len)
            d = max(d, 1.0 / max(fps, 1))  # 最低1フレーム
            d = min(d, seg_len * 0.9, acc * 0.9)
            off = acc - d
            filters.append(f"[{cur_v}][v{k}]xfade=transition=fade:duration={d:.3f}:offset={off:.3f}[vx{k}]")
            filters.append(f"[{cur_a}][a{k}]acrossfade=d={d:.3f}[ax{k}]")
            cur_v, cur_a = f"vx{k}", f"ax{k}"
            acc = acc + seg_len - d
        filters.append(f"[{cur_v}]null[v]")
        filters.append(f"[{cur_a}]anull[a]")
    cmd = ["ffmpeg", "-y", "-i", str(input_video),
           "-filter_complex", ";".join(filters),
           "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", str(out_path)]
    _run_ffmpeg(cmd, main_dur, progress_cb, base_pct=0, span_pct=30)


def build_and_run(input_video: Path, thumb: Path, output: Path, cfg: dict, progress_cb=None):
    info = probe(input_video)
    vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    width = int(vstream["width"])
    height = int(vstream["height"])
    # fps: "30/1" 形式を評価
    num, _, den = str(vstream.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1)
    if fps <= 0 or fps > 120:
        fps = 30.0
    total = float(info["format"]["duration"])

    trim_start = parse_time(cfg.get("trim_start", 0))
    if cfg.get("end") is not None:
        end = parse_time(cfg["end"])
    else:
        end = total - parse_time(cfg.get("trim_tail", 0))
    main_dur = end - trim_start
    if main_dur <= 0:
        raise ValueError(f"本編が0秒以下です (total={total:.1f}s, start={trim_start}, end={end})")

    dissolve = float(cfg.get("dissolve", 0.8))
    crf = int(cfg.get("crf", 18))
    preset = cfg.get("preset", "fast")

    # --- OP/ED素材の種類 (画像 or 動画) ---
    asset_type = cfg.get("asset_type")
    asset_info = None
    if asset_type is None:
        try:
            asset_info = probe(thumb)
            asset_type = "video"
        except Exception:  # noqa: BLE001
            asset_type = "image"
    elif asset_type == "video":
        asset_info = probe(thumb)
    if asset_type == "video":
        # 動画素材はそのままの長さで頭とお尻に付ける
        intro = outro = float(asset_info["format"]["duration"])
        asset_has_audio = any(s["codec_type"] == "audio" for s in asset_info["streams"])
    else:
        intro = float(cfg.get("intro_sec", 3))
        outro = float(cfg.get("outro_sec", 3))
        asset_has_audio = False

    # --- 無音カット (本編の中間ファイルを作る) ---
    main_input = input_video
    main_start, main_end = trim_start, end
    base_pct, span_pct = 0, 100
    tmp_cut = None
    if cfg.get("silence_cut", False) and has_audio:
        min_sec = float(cfg.get("silence_sec", 3.0))
        db = int(cfg.get("silence_db", -38))
        keeps, cuts = _keep_segments(detect_silences(input_video, min_sec, db),
                                     trim_start, end, min_sec)
        if cuts:
            tmp_cut = output.with_name(output.stem + "_maincut.mp4")
            _build_main_cut(input_video, keeps, width, height, fps, tmp_cut,
                            max(10, crf - 3), preset, progress_cb, main_dur,
                            dissolve=dissolve)
            main_input = tmp_cut
            main_start = 0.0
            main_end = float(probe(tmp_cut)["format"]["duration"])
            base_pct, span_pct = 30, 70
            removed = sum(e - s for s, e in cuts)
            print(f"  無音カット: {len(cuts)}箇所 / 合計 {removed:.1f}秒を除去")
    main_dur2 = main_end - main_start

    if dissolve >= intro or dissolve >= outro or dissolve >= main_dur2:
        if tmp_cut:
            tmp_cut.unlink(missing_ok=True)
        raise ValueError("ディゾルブ秒数がクリップ長より長いです。dissolveを短くしてください")

    # xfade の offset は「1本目のクリップ長 - ディゾルブ」
    offset1 = intro - dissolve
    # 1回目合成後の長さ = intro + main - dissolve。その末尾 - dissolve が2回目の offset
    offset2 = intro + main_dur2 - 2 * dissolve

    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"

    filters = []
    # --- 映像 ---
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS[v0]")
    filters.append(f"[1:v]trim=start={main_start}:end={main_end},setpts=PTS-STARTPTS,{scale}[v1]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS[v2]")
    filters.append(f"[v0][v1]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

    # --- 音声 ---
    if asset_has_audio:
        # 動画素材の音声をそのまま使う (無音トラックは使わない)
        filters.append(f"[0:a]atrim=duration={intro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a0]")
        filters.append(f"[2:a]atrim=duration={outro},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a2]")
    else:
        # 無音トラック(入力3) を intro/outro 用に2つに分ける
        filters.append("[3:a]asplit=2[sil1][sil2]")
        filters.append(f"[sil1]atrim=duration={intro},asetpts=PTS-STARTPTS[a0]")
        filters.append(f"[sil2]atrim=duration={outro},asetpts=PTS-STARTPTS[a2]")
    if has_audio:
        filters.append(f"[1:a]atrim=start={main_start}:end={main_end},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a1]")
    else:
        # 音声なし動画の場合は無音の本編を作る
        filters.append(f"anullsrc=cl=stereo:r=48000,atrim=duration={main_dur2},asetpts=PTS-STARTPTS[a1]")
    filters.append(f"[a0][a1]acrossfade=d={dissolve}[ax]")
    filters.append(f"[ax][a2]acrossfade=d={dissolve}[aout]")

    filter_complex = ";".join(filters)
    silent_dur = intro + main_dur2 + outro

    if asset_type == "video":
        asset_in = ["-i", str(thumb)]
    else:
        asset_in = ["-loop", "1", "-framerate", str(fps), "-t", str(intro), "-i", str(thumb)]
        asset_in_outro = ["-loop", "1", "-framerate", str(fps), "-t", str(outro), "-i", str(thumb)]
    cmd = ["ffmpeg", "-y"]
    cmd += asset_in
    cmd += ["-i", str(main_input)]
    cmd += asset_in if asset_type == "video" else asset_in_outro
    cmd += [
        "-f", "lavfi", "-t", str(silent_dur), "-i", "anullsrc=cl=stereo:r=48000",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    print("実行:", " ".join(cmd[:6]), "...")
    print(f"  本編: {main_start:.1f}s 〜 {main_end:.1f}s (残り {main_dur2:.1f}s) / "
          f"intro {intro}s + outro {outro}s / dissolve {dissolve}s / 素材={asset_type}")
    expected = intro + main_dur2 + outro - 2 * dissolve
    try:
        _run_ffmpeg(cmd, expected, progress_cb, base_pct=base_pct, span_pct=span_pct)
    finally:
        if tmp_cut:
            tmp_cut.unlink(missing_ok=True)
    final = probe(output)
    print(f"  完了: {output} (長さ {float(final['format']['duration']):.1f}s, "
          f"想定 {expected:.1f}s)")


def main():
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
    args = ap.parse_args()

    cfg = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # CLI指定があれば config より優先
    for key, val in [("trim_start", args.trim_start), ("trim_tail", args.trim_tail),
                     ("end", args.end), ("intro_sec", args.intro_sec),
                     ("outro_sec", args.outro_sec), ("dissolve", args.dissolve),
                     ("asset_type", args.asset_type), ("silence_sec", args.silence_sec)]:
        if val is not None:
            cfg[key] = val
    if args.silence_cut:
        cfg["silence_cut"] = True

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
