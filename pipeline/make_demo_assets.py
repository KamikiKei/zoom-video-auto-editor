#!/usr/bin/env python3
"""デモ用の合成録画+VTT+素材を pipeline/demo_inbox/demo-001/ に作る。

音声タイムライン (34秒):
  0–5s   運営の声 (330Hz)   … VTTで「ご紹介します」と話す
  5–9s   無音 (4秒)
  9–20s  登壇者の声 (440Hz) … 開始点はここ (9.0s) になる想定
  20–24.5s 無音 (4.5秒)     … 本編中の無音カット対象
  24.5–34s 登壇者の声       … 28.5s以降はVTT上トラブル対応の発話
想定: 開始点 9.0s / 保持区間 [9,20] [24.5,28.5] [33.5,34]
      (無音 [20,24.5] と AI検出モックのトラブル候補 [28.5,33.5] をカット。
       33.5 は最終キューの終わりで、動画末尾の 0.5 秒は保持に残る)
      / 出力 3+14.5+3-1.6 = 18.9秒
"""
import json
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEST = BASE / "demo_inbox" / "demo-001"

VTT = """WEBVTT

1
00:00:00.000 --> 00:00:05.000
運営: 本日はPaper & Hacksにご参加いただきありがとうございます。それでは本日の登壇者をご紹介します。

2
00:00:09.000 --> 00:00:20.000
登壇者: はい、よろしくお願いします。本日はデモ発表として、パイプラインの動作をお見せします。

3
00:00:24.500 --> 00:00:28.500
登壇者: まず背景から説明します。

4
00:00:28.500 --> 00:00:33.500
登壇者: あれ、画面が見えますか。聞こえますか。えーと、少々お待ちください。
"""

META = {
    "title": "デモ: パイプライン動作確認",
    "talk_title": "パイプラインのデモ発表",
    "presenter": "デモ登壇者",
    "session_no": 999,
    "kind": "regular",
    "started_at": "2026-11-21T20:00:00+09:00",
    "audience": "LLMに興味のある方",
    "level": "初級〜中級",
    "category": "デモ",
    "about": "デモ用のイベント内容です。",
    "papers": "Demo Paper (2026)",
    "notes": "デモ用の注意・連絡事項です。",
}

AUDIO_EXPR = (
    "if(lt(t,5),0.3*sin(2*PI*330*t),"
    "if(lt(t,9),0,"
    "if(lt(t,20),0.3*sin(2*PI*440*t),"
    "if(lt(t,24.5),0,0.3*sin(2*PI*440*t)))))"
)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=34",
         "-f", "lavfi", "-i", f"aevalsrc='{AUDIO_EXPR}':s=48000:d=34",
         "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(DEST / "video.mp4")],
        check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=navy:s=640x360",
         "-frames:v", "1", str(DEST / "thumb.png")],
        check=True)
    (DEST / "transcript.vtt").write_text(VTT, encoding="utf-8")
    (DEST / "meta.json").write_text(
        json.dumps(META, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"デモ素材を作成しました: {DEST}")


if __name__ == "__main__":
    main()
