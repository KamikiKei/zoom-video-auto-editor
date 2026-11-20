#!/bin/bash
cd ~/workspace/video-auto-editor/bench
cat > cfg_fast.json <<'JSON'
{"crf": 18, "dissolve": 0.8, "intro_sec": 3, "outro_sec": 3, "preset": "fast",
 "trim_start": 12, "trim_tail": 8, "silence_cut": true, "silence_sec": 3.0, "silence_db": -38}
JSON
cat > cfg_veryfast.json <<'JSON'
{"crf": 18, "dissolve": 0.8, "intro_sec": 3, "outro_sec": 3, "preset": "veryfast",
 "trim_start": 12, "trim_tail": 8, "silence_cut": true, "silence_sec": 3.0, "silence_db": -38}
JSON
t0=$(date +%s)
python3 ../edit.py src_1080p_180s.mp4 thumb.png -o out_fast.mp4 --config cfg_fast.json >> bench_results.txt 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=fast (testsrc2) : wall $((t1 - t0))s" >> bench_results.txt
t0=$(date +%s)
python3 ../edit.py zoomlike_1080p_180s.mp4 thumb.png -o out_zoomlike_fast.mp4 --config cfg_fast.json >> bench_results_zoomlike.txt 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=fast (zoomlike) : wall $((t1 - t0))s" >> bench_results_zoomlike.txt
t0=$(date +%s)
python3 ../edit.py zoomlike_1080p_180s.mp4 thumb.png -o out_zoomlike_veryfast.mp4 --config cfg_veryfast.json >> bench_results_zoomlike.txt 2>&1
t1=$(date +%s); echo "--- edit.py full pipeline preset=veryfast (zoomlike) : wall $((t1 - t0))s" >> bench_results_zoomlike.txt
ls -la out_fast.mp4 out_zoomlike_fast.mp4 out_zoomlike_veryfast.mp4 >> bench_results_zoomlike.txt
# 古い DONE/ALLDONE 行を整理: 末尾に最終マーカーを追加
echo EDITPY_DONE >> bench_results.txt
echo EDITPY_DONE >> bench_results_zoomlike.txt
