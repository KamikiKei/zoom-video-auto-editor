#!/bin/bash
# 1080p30 / 180秒 / 音声は20秒発話+10秒無音の繰り返し（無音カット対象あり）
set -e
cd ~/workspace/video-auto-editor/bench
ffmpeg -y -v error \
  -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=180" \
  -f lavfi -i "aevalsrc='if(lt(mod(t,30),20),sin(2*PI*440*t)*0.3,0)':s=48000:d=180" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p \
  -c:a aac -b:a 128k -shortest src_1080p_180s.mp4
ffmpeg -y -v error -f lavfi -i "color=c=0x1a2b4c:s=1920x1080:d=1" -frames:v 1 thumb.png
ffprobe -v error -show_entries format=duration -of csv=p=0 src_1080p_180s.mp4
