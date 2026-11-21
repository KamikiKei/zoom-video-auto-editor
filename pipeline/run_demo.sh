#!/bin/bash
# パイプライン骨格の通しデモ:
#   合成録画を inbox に置く -> デーモン (demo) が 取り込み -> ジョブ作成
#   -> 確認依頼① (console) -> 書き出し再現 -> モックYouTube -> 確認依頼②
#   -> 最後にエディタ (app.py) でジョブが開けることを確認する
set -e
cd "$(dirname "$0")/.."

# 前回のデモジョブを掃除
python3 - <<'PY'
import json, shutil
from pathlib import Path
st = Path("pipeline/demo_state.json")
if st.exists():
    for rec in json.loads(st.read_text(encoding="utf-8"))["recordings"].values():
        jid = rec.get("job_id")
        if jid:
            shutil.rmtree(Path("jobs") / jid, ignore_errors=True)
PY
rm -rf pipeline/demo_outbox pipeline/demo_work
rm -f pipeline/demo_state.json

python3 pipeline/make_demo_assets.py

unset SLACK_WEBHOOK_URL   # デモはコンソール通知で
python3 -m pipeline.daemon --once --demo \
  --inbox pipeline/demo_inbox \
  --outbox pipeline/demo_outbox \
  --state pipeline/demo_state.json \
  --work pipeline/demo_work

echo "== 状態と成果物の確認 =="
python3 - <<'PY'
import json
from pathlib import Path
st = json.loads(Path("pipeline/demo_state.json").read_text(encoding="utf-8"))
rec = st["recordings"]["demo-001"]
assert rec["status"] == "done", rec
assert rec["youtube_url"].startswith("https://mock-youtube.example.invalid/"), rec
job = Path("jobs") / rec["job_id"]
assert (job / "output.mp4").exists(), job
assert list(Path("pipeline/demo_outbox").glob("*.mp4")), "outbox に動画が無い"
print("state: done / youtube_url:", rec["youtube_url"])
print("editor url:", rec["editor_url"])
print("job:", rec["job_id"])
PY

echo "== エディタ連携の確認 (app.py でジョブが開けるか) =="
JOB=$(python3 -c "import json;print(json.load(open('pipeline/demo_state.json'))['recordings']['demo-001']['job_id'])")
python3 app.py --port 8799 > /tmp/pipeline_demo_app.log 2>&1 &
SRV=$!
trap "kill $SRV 2>/dev/null || true" EXIT
sleep 2
curl -s "http://localhost:8799/api/jobinfo/$JOB" | python3 -c "
import sys, json
info = json.load(sys.stdin)
assert info['has_video'] and info['has_thumb'] and info['plan'], info
assert info['plan']['trim_start'] == 9.0, info['plan']
assert info['diarization']['presenter_guess'] == 1, info.get('diarization')
print('jobinfo OK: plan.trim_start=9.0 / presenter_guess=1 / stage=', info.get('stage'))
"
curl -s "http://localhost:8799/api/status/$JOB" | grep -q '"stage": "done"' \
  && echo "status OK: done"
curl -s "http://localhost:8799/?job=$JOB" | grep -q "initFromQuery" \
  && echo "editor page OK (?job= の初期化コードあり)"

echo "DEMO OK"
