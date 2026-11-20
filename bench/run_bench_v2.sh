#!/bin/bash
# ベンチマーク v2: 180秒 1080p30 素材 (testsrc2=エンコードが重い保守的な素材)
cd ~/workspace/video-auto-editor/bench
LOG=bench_results.txt
{
echo "=== benchmark run v2 $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) / nproc=$(nproc) / shared CPU quota 1.5"
echo "source: src_1080p_180s.mp4 (180s, 1920x1080, 30fps, h264 crf23, testsrc2)"
echo "--- raw fast threads=1 : wall 322s / frame=5400 fps=17 speed=0.559x (v1 run 2026-11-20 measured)"
} > $LOG
run() {
  local label="$1"; shift
  local t0=$(date +%s)
  ffmpeg -y -v info "$@" 2> ff_tmp.log
  local t1=$(date +%s)
  local el=$((t1 - t0))
  local last=$(tr '\r' '\n' < ff_tmp.log | grep "speed=" | tail -1 | tr -s ' ')
  echo "--- $label : wall ${el}s / $last" >> $LOG
}
run "raw fast threads=2" -i src_1080p_180s.mp4 -c:v libx264 -crf 18 -preset fast -threads 2 -c:a aac -b:a 192k -f null -
run "raw fast threads=default" -i src_1080p_180s.mp4 -c:v libx264 -crf 18 -preset fast -c:a aac -b:a 192k -f null -
run "raw medium threads=default" -i src_1080p_180s.mp4 -c:v libx264 -crf 18 -preset medium -c:a aac -b:a 192k -f null -
run "raw veryfast threads=default" -i src_1080p_180s.mp4 -c:v libx264 -crf 18 -preset veryfast -c:a aac -b:a 192k -f null -
run "silencedetect only" -i src_1080p_180s.mp4 -vn -af "silencedetect=noise=-38dB:d=3" -f null -
cat > cfg_fast.json <<'JSON'
{"crf": 18, "dissolve": 0.8, "intro_sec": 3, "outro_sec": 3, "preset": "fast",
 "trim_start": 12, "trim_tail": 8, "silence_cut": true, "silence_sec": 3.0, "silence_db": -38}
JSON
cat > cfg_medium.json <<'JSON'
{"crf": 18, "dissolve": 0.8, "intro_sec": 3, "outro_sec": 3, "preset": "medium",
 "trim_start": 12, "trim_tail": 8, "silence_cut": true, "silence_sec": 3.0, "silence_db": -38}
JSON
t0=$(date +%s)
python3 ../edit.py src_1080p_180s.mp4 thumb.png -o out_fast.mp4 --config cfg_fast.json >> $LOG 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=fast : wall $((t1 - t0))s" >> $LOG
t0=$(date +%s)
python3 ../edit.py src_1080p_180s.mp4 thumb.png -o out_medium.mp4 --config cfg_medium.json >> $LOG 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=medium : wall $((t1 - t0))s" >> $LOG
ls -la out_fast.mp4 out_medium.mp4 >> $LOG
echo DONE >> $LOG
