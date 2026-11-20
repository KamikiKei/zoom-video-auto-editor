#!/bin/bash
# Zoom風素材: 静止スライド背景 + 右下に小さな動く話者映像(PiP) + 音声(20秒発話+10秒無音)
set -e
cd ~/workspace/video-auto-editor/bench
ffmpeg -y -v error \
  -loop 1 -framerate 30 -i thumb.png \
  -f lavfi -i "testsrc2=size=320x180:rate=30:duration=180" \
  -f lavfi -i "aevalsrc='if(lt(mod(t,30),20),sin(2*PI*440*t)*0.3,0)':s=48000:d=180" \
  -filter_complex "[0:v][1:v]overlay=W-w-16:H-h-16,format=yuv420p[v]" \
  -map "[v]" -map 2:a \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p \
  -c:a aac -b:a 128k -shortest zoomlike_1080p_180s.mp4
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 zoomlike_1080p_180s.mp4
