#!/bin/bash
# サーバ起動 -> アップロード -> 話者解析 -> サンプル取得 -> 編集 -> ダウンロード の一気通貫テスト
set -e
cd "$(dirname "$0")/.."
python3 app.py --port 8765 > /tmp/app.log 2>&1 &
SRV=$!
trap "kill $SRV 2>/dev/null || true" EXIT
sleep 2

JOB=$(curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://localhost:8765/api/start | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "job=$JOB"
curl -s -X PUT --data-binary @test/synth_meeting.mp4 http://localhost:8765/api/upload/$JOB/video; echo
curl -s -X POST -H 'Content-Type: application/json' -d '{"speakers":2}' http://localhost:8765/api/diarize/$JOB > /dev/null
for i in $(seq 1 20); do
  sleep 2
  S=$(curl -s http://localhost:8765/api/status/$JOB)
  echo "$S" | grep -q '"stage": "diarized"' && { echo "diarized OK"; break; }
done
curl -s -o /tmp/sample0.mp3 -w "sample0 -> %{http_code} %{size_download}bytes\n" http://localhost:8765/api/sample/$JOB/0
curl -s -X PUT --data-binary @test/thumb.png http://localhost:8765/api/upload/$JOB/thumb; echo
curl -s -X POST -H 'Content-Type: application/json' -d '{"trim_start":7,"end":30,"intro_sec":2,"outro_sec":2,"dissolve":0.5}' http://localhost:8765/api/process/$JOB; echo
for i in $(seq 1 30); do
  sleep 3
  S=$(curl -s http://localhost:8765/api/status/$JOB)
  echo "$S" | grep -q '"stage": "done"' && { echo "process done"; break; }
  echo "$S" | grep -q '"stage": "error"' && { echo "ERROR: $S"; exit 1; }
done
curl -s -o /tmp/final_edited.mp4 -w "download -> %{http_code} %{size_download}bytes\n" http://localhost:8765/api/download/$JOB
echo -n "final duration (expect 26.0 = 2+23+2-0.5*2): "
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/final_edited.mp4
echo "E2E OK"
