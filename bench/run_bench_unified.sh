#!/bin/bash
# ベンチ: 統合分割方式 (新) vs 従来方式 (旧 ddcf66b: 本編カット + 最終パス再エンコード)
#
# 条件 (実素材相当の合成ケース):
#   - 元動画: 1920x1080 / 25fps / 450秒。音声は 7.2秒トーン + 1.8秒無音 の
#     繰り返しで、無音カット (1.5秒以上) により保持区間が約50個できる
#   - 素材: 1080p・音声つき動画 5秒 (Canva書き出し相当)。intro=outro=5秒
#   - dissolve 0.8 / libx264 veryfast / crf 18 (このVMはGPU無し)
#   - 旧版は git の ddcf66b から取り出した edit.py で render する
#
# 測るもの: render の wall time (前後比較は相対値)、出力尺 vs 想定尺、
# 映像/音声ストリーム尺の差 (音声ズレ確認)。
# 実行環境の絶対値は Colab と異なるため、比率で見ること。
set -e
cd "$(dirname "$0")/.."
ROOT=$PWD
# 作業場所は大きなディスクに取る (/tmp が小さな tmpfs の環境では
# 1080p の断片だけで容量不足になり、計測が壊れるため)
WORK=$(mktemp -d "$HOME/bench_unified_XXXXXX")
trap "rm -rf $WORK" EXIT
cd "$WORK"
echo "workdir: $WORK"

echo "== 素材生成 (1080p / 450s / 保持区間約50) =="
ffmpeg -y -v error -f lavfi -i testsrc2=size=1920x1080:rate=25:duration=450 \
  -f lavfi -i "aevalsrc='if(lt(mod(t,9),7.2),0.3*sin(2*PI*440*t),0)':s=48000:d=450" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac src450.mp4
ffmpeg -y -v error -f lavfi -i testsrc=size=1920x1080:rate=25:duration=5 \
  -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=5" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac asset5.mp4

echo "== plan =="
python3 "$ROOT/edit.py" plan --video src450.mp4 --thumb asset5.mp4 \
  --silence-cut --silence-sec 1.5 --dissolve 0.8 --out plan.json > plan.log
python3 - <<'PY'
import json
p = json.load(open("plan.json"))
keeps = p["silence"]["keeps"]
print(f"keeps={len(keeps)} expected_duration={p['expected_duration']}")
assert len(keeps) >= 45, keeps
open("expected.txt", "w").write(str(p["expected_duration"]))
PY
EXPECTED=$(cat expected.txt)

# 旧版 edit.py (ddcf66b) を取り出す
mkdir old && git -C "$ROOT" show ddcf66b:edit.py > old/edit.py

run_one() { # $1=edit.py のパス $2=出力 $3=ラベル
  local start end
  start=$(date +%s.%N)
  if ! python3 "$1" render --plan plan.json -o "$2" --preset veryfast > "$3.log" 2>&1; then
    echo "RENDER FAILED ($3)"; tail -30 "$3.log"; exit 1
  fi
  [ -s "$2" ] || { echo "OUTPUT MISSING ($3)"; exit 1; }
  end=$(date +%s.%N)
  python3 -c "print(f'{$end - $start:.1f}')"
}

echo "== render 旧方式 (ddcf66b: 本編カット + 最終パス) =="
T_OLD=$(run_one old/edit.py out_old.mp4 old)
echo "old: ${T_OLD}s"
grep -m1 "分割方式\|従来方式" old.log || true

echo "== render 新方式 (統合分割方式) =="
T_NEW=$(run_one "$ROOT/edit.py" out_new.mp4 new)
echo "new: ${T_NEW}s"
grep -m1 "統合分割方式" new.log || true

echo "== 検証 (尺・音声ズレ) =="
python3 - "$EXPECTED" "$T_OLD" "$T_NEW" <<'PY'
import json, subprocess, sys

expected, t_old, t_new = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])

def streams(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration", "-of", "json", path],
        capture_output=True, text=True)
    data = json.loads(r.stdout or "{}")
    assert "streams" in data, (path, r.stdout, r.stderr)
    return {s["codec_type"]: float(s["duration"])
            for s in data["streams"]}

for label, path in (("old", "out_old.mp4"), ("new", "out_new.mp4")):
    d = streams(path)
    total = max(d.values())
    av_diff = abs(d["video"] - d["audio"])
    print(f"{label}: video={d['video']:.2f}s audio={d['audio']:.2f}s "
          f"|expected-actual|={abs(total - expected):.2f}s av_diff={av_diff:.3f}s")
    assert abs(total - expected) <= 0.6, (label, total, expected)
    if label == "new":
        # 新方式は映像・音声ストリーム長が一致すること。旧方式の最終パスは
        # 末尾で映像が約0.27s 長くなる既存の癖があるため参考表示のみ
        assert av_diff <= 0.15, (label, av_diff)

print(f"\nrender time: old={t_old:.1f}s new={t_new:.1f}s "
      f"ratio={t_new / t_old:.2f} (speedup {t_old / t_new:.2f}x)")
PY
echo "BENCH OK"
