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


def build_and_run(input_video: Path, thumb: Path, output: Path, cfg: dict):
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

    intro = float(cfg.get("intro_sec", 3))
    outro = float(cfg.get("outro_sec", 3))
    dissolve = float(cfg.get("dissolve", 0.8))
    crf = int(cfg.get("crf", 18))
    preset = cfg.get("preset", "medium")

    if dissolve >= intro or dissolve >= outro or dissolve >= main_dur:
        raise ValueError("ディゾルブ秒数がクリップ長より長いです。dissolveを短くしてください")

    # xfade の offset は「1本目のクリップ長 - ディゾルブ」
    offset1 = intro - dissolve
    # 1回目合成後の長さ = intro + main - dissolve。その末尾 - dissolve が2回目の offset
    offset2 = intro + main_dur - 2 * dissolve

    scale = f"scale={width}:{height}:force_original_aspect_ratio=disable,setsar=1,fps={fps},format=yuv420p"

    filters = []
    # --- 映像 ---
    filters.append(f"[0:v]{scale},trim=duration={intro},setpts=PTS-STARTPTS[v0]")
    filters.append(f"[1:v]trim=start={trim_start}:end={end},setpts=PTS-STARTPTS,{scale}[v1]")
    filters.append(f"[2:v]{scale},trim=duration={outro},setpts=PTS-STARTPTS[v2]")
    filters.append(f"[v0][v1]xfade=transition=fade:duration={dissolve}:offset={offset1}[vx]")
    filters.append(f"[vx][v2]xfade=transition=fade:duration={dissolve}:offset={offset2}[vout]")

    # --- 音声 ---
    # 無音トラック(入力3) を intro/outro 用に2つに分ける
    filters.append("[3:a]asplit=2[sil1][sil2]")
    filters.append(f"[sil1]atrim=duration={intro},asetpts=PTS-STARTPTS[a0]")
    filters.append(f"[sil2]atrim=duration={outro},asetpts=PTS-STARTPTS[a2]")
    if has_audio:
        filters.append(f"[1:a]atrim=start={trim_start}:end={end},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a1]")
    else:
        filters.append(f"[sil1]anull[a1]")  # 音声なし動画用フォールバック(実際は下で差し替え)
        # 音声なしの場合は無音本編を作る
        filters[-1] = f"anullsrc=cl=stereo:r=48000,atrim=duration={main_dur},asetpts=PTS-STARTPTS[a1]"
    filters.append(f"[a0][a1]acrossfade=d={dissolve}[ax]")
    filters.append(f"[ax][a2]acrossfade=d={dissolve}[aout]")

    filter_complex = ";".join(filters)
    silent_dur = intro + main_dur + outro

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(fps), "-t", str(intro), "-i", str(thumb),
        "-i", str(input_video),
        "-loop", "1", "-framerate", str(fps), "-t", str(outro), "-i", str(thumb),
        "-f", "lavfi", "-t", str(silent_dur), "-i", "anullsrc=cl=stereo:r=48000",
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    print("実行:", " ".join(cmd[:6]), "...")
    print(f"  本編: {trim_start:.1f}s 〜 {end:.1f}s (残り {main_dur:.1f}s) / "
          f"intro {intro}s + outro {outro}s / dissolve {dissolve}s")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        raise RuntimeError("ffmpeg が失敗しました")
    final = probe(output)
    print(f"  完了: {output} (長さ {float(final['format']['duration']):.1f}s, "
          f"想定 {intro + main_dur + outro - 2 * dissolve:.1f}s)")


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
    args = ap.parse_args()

    cfg = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # CLI指定があれば config より優先
    for key, val in [("trim_start", args.trim_start), ("trim_tail", args.trim_tail),
                     ("end", args.end), ("intro_sec", args.intro_sec),
                     ("outro_sec", args.outro_sec), ("dissolve", args.dissolve)]:
        if val is not None:
            cfg[key] = val

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
