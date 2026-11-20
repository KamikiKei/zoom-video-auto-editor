#!/bin/bash
# 残りのベンチマーク: testsrc2 veryfast + 無音検出 + edit.py fast、続けてZoom風素材
cd ~/workspace/video-auto-editor/bench
LOG=bench_results.txt
run() {
  local label="$1"; shift
  local t0=$(date +%s)
  ffmpeg -y -v info "$@" 2> ff_tmp.log
  local t1=$(date +%s)
  local el=$((t1 - t0))
  local last=$(tr '\r' '\n' < ff_tmp.log | grep "speed=" | tail -1 | tr -s ' ')
  echo "--- $label : wall ${el}s / $last" >> $LOG
}
run "raw veryfast threads=default" -i src_1080p_180s.mp4 -c:v libx264 -crf 18 -preset veryfast -c:a aac -b:a 192k -f null -
run "silencedetect only" -i src_1080p_180s.mp4 -vn -af "silencedetect=noise=-38dB:d=3" -f null -
t0=$(date +%s)
python3 ../edit.py src_1080p_180s.mp4 thumb.png -o out_fast.mp4 --config cfg_fast.json >> $LOG 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=fast : wall $((t1 - t0))s" >> $LOG
echo DONE >> $LOG
# --- Zoom風素材 ---
./gen_zoomlike.sh >> $LOG 2>&1
LOG2=bench_results_zoomlike.txt
{
echo "=== zoomlike benchmark run $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
echo "source: zoomlike_1080p_180s.mp4 (180s, 1920x1080, 30fps, static slide + PiP speaker)"
} > $LOG2
run2() {
  local label="$1"; shift
  local t0=$(date +%s)
  ffmpeg -y -v info "$@" 2> ff_tmp2.log
  local t1=$(date +%s)
  local el=$((t1 - t0))
  local last=$(tr '\r' '\n' < ff_tmp2.log | grep "speed=" | tail -1 | tr -s ' ')
  echo "--- $label : wall ${el}s / $last" >> $LOG2
}
run2 "raw fast threads=default" -i zoomlike_1080p_180s.mp4 -c:v libx264 -crf 18 -preset fast -c:a aac -b:a 192k -f null -
run2 "raw veryfast threads=default" -i zoomlike_1080p_180s.mp4 -c:v libx264 -crf 18 -preset veryfast -c:a aac -b:a 192k -f null -
t0=$(date +%s)
python3 ../edit.py zoomlike_1080p_180s.mp4 thumb.png -o out_zoomlike_fast.mp4 --config cfg_fast.json >> $LOG2 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=fast : wall $((t1 - t0))s" >> $LOG2
ls -la out_fast.mp4 out_zoomlike_fast.mp4 >> $LOG2
echo ALLDONE >> $LOG2
echo ALLDONE >> $LOG
