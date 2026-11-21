#!/bin/bash
# edit.py 再構成 (plan/render分離・1パス化・エンコーダ抽象化) の回帰テスト
# 期待尺はすべて計算値。旧版との一致は再構成時に実測で確認済み (11.0 / 17.5 / 11.0)。
set -e
cd "$(dirname "$0")/.."
THUMB=test/thumb.png
WORK=$(mktemp -d)
trap "rm -rf $WORK" EXIT
cd "$WORK"
E="python3 $OLDPWD/edit.py"

# --- 素材生成 ---
# plain: 20秒の連続音声つき動画
ffmpeg -y -v error -f lavfi -i testsrc2=size=320x180:rate=30:duration=20 \
  -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=20" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac plain.mp4
# sil: 音声5s / 無音5s / 音声4s / 無音2.5s / 音声3.5s (=20s)
#   3秒以上の無音は [5,10] のみカットされ、2.5s無音は残る -> 保持区間 [0,5] [10,20]
ffmpeg -y -v error -f lavfi -i testsrc2=size=320x180:rate=30:duration=20 \
  -f lavfi -i "aevalsrc='if(lt(t,5),0.3*sin(2*PI*440*t),if(lt(t,10),0,if(lt(t,14),0.3*sin(2*PI*440*t),if(lt(t,16.5),0,0.3*sin(2*PI*440*t)))))':s=48000:d=20" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac sil.mp4
# asset: 音声つき2秒の動画素材
ffmpeg -y -v error -f lavfi -i testsrc=size=640x360:rate=30:duration=2 \
  -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=2" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac asset.mp4
# sample.vtt: 運営の紹介 (0-4.5s) の直後に登壇者が話し始める (5s-)
cat > sample.vtt <<'VTT'
WEBVTT

1
00:00:00.000 --> 00:00:04.500
運営: 本日はPaper & Hacksにご参加いただきありがとうございます。それでは本日の登壇者をご紹介します。

2
00:00:05.000 --> 00:00:15.000
登壇者: はい、よろしくお願いします。本日はRAGについて話します。

3
00:00:15.500 --> 00:00:19.000
登壇者: まず背景から説明します。
VTT

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
check() { # $1=実測 $2=期待 $3=ラベル
  python3 -c "import sys; sys.exit(0 if abs(float('$1') - float('$2')) <= 0.2 else 1)" \
    && echo "OK   $3: $1 (expect $2)" || { echo "FAIL $3: $1 (expect $2)"; exit 1; }
}

cat > cfgA.json <<'J'
{"trim_start": 5, "end": 13, "intro_sec": 2, "outro_sec": 2, "dissolve": 0.5}
J
cat > cfgB.json <<'J'
{"silence_cut": true, "silence_sec": 3.0, "intro_sec": 2, "outro_sec": 2, "dissolve": 0.5}
J
cat > cfgC.json <<'J'
{"trim_start": 1, "end": 9, "dissolve": 0.5}
J

echo "== Case A: 画像素材 (2 + 8 + 2 - 0.5*2 = 11.0) =="
$E plain.mp4 "$OLDPWD/$THUMB" -o outA.mp4 --config cfgA.json > /dev/null 2>&1
check "$(dur outA.mp4)" 11.0 "A 1pass"

echo "== Case B: 無音カット (保持15s - つなぎ0.5 + 2 + 2 - 0.5*2 = 17.5) =="
$E sil.mp4 "$OLDPWD/$THUMB" -o outB.mp4 --config cfgB.json > /dev/null 2>&1
check "$(dur outB.mp4)" 17.5 "B 1pass"
$E sil.mp4 "$OLDPWD/$THUMB" -o outB2.mp4 --config cfgB.json --two-pass > /dev/null 2>&1
check "$(dur outB2.mp4)" 17.5 "B 2pass(forced fallback)"

echo "== Case C: 動画素材・音声あり2秒 (2 + 8 + 2 - 0.5*2 = 11.0) =="
$E plain.mp4 asset.mp4 -o outC.mp4 --config cfgC.json > /dev/null 2>&1
check "$(dur outC.mp4)" 11.0 "C 1pass"

echo "== Case D: plan -> render 往復 (Case B と同じ 17.5) =="
$E plan --video sil.mp4 --thumb "$OLDPWD/$THUMB" --silence-cut --silence-sec 3 \
  --intro-sec 2 --outro-sec 2 --dissolve 0.5 --out planB.json > /dev/null
$E render --plan planB.json -o outD.mp4 > /dev/null 2>&1
check "$(dur outD.mp4)" 17.5 "D plan->render"

echo "== Case E: plan に文字起こし (開始点=登壇者の5.0s, 保持[10,20] -> 13.0) =="
$E plan --video sil.mp4 --transcript sample.vtt --thumb "$OLDPWD/$THUMB" \
  --silence-cut --silence-sec 3 --intro-sec 2 --outro-sec 2 --dissolve 0.5 \
  --out planT.json > /dev/null
python3 - <<'PY'
import json
p = json.load(open("planT.json"))
assert p["trim_start"] == 5.0 and p["trim_start_source"] == "transcript"
assert p["speakers"]["presenter_guess"] == 1
assert p["silence"]["keeps"] == [[10.0, 20.0]]
assert p["expected_duration"] == 13.0
print("OK   E plan JSON (trim/transcript, presenter, keeps, expected)")
PY
$E render --plan planT.json -o outE.mp4 > /dev/null 2>&1
check "$(dur outE.mp4)" 13.0 "E render"

echo "== Case F: encoder auto (GPU無し環境では libx264 にフォールバック) =="
$E plain.mp4 "$OLDPWD/$THUMB" -o outF.mp4 --config cfgA.json --encoder auto > enc.log 2>&1
grep -q "エンコーダ:" enc.log
check "$(dur outF.mp4)" 11.0 "F encoder auto"

echo "== Case G: 複数登壇者 (presenters で開始点が変わる) =="
$E plan --video sil.mp4 --transcript sample.vtt --thumb "$OLDPWD/$THUMB" \
  --silence-cut --silence-sec 3 --intro-sec 2 --outro-sec 2 --dissolve 0.5 \
  --presenters 0 --out planG0.json > /dev/null
$E plan --video sil.mp4 --transcript sample.vtt --thumb "$OLDPWD/$THUMB" \
  --silence-cut --silence-sec 3 --intro-sec 2 --outro-sec 2 --dissolve 0.5 \
  --presenters 0,1 --out planG01.json > /dev/null
python3 - <<'PY'
import json
p0 = json.load(open("planG0.json"))
# 話者0 (運営) は5秒以上の発言が無いので、最初の発言の頭 0.0 が開始点
assert p0["presenters"] == [0] and p0["trim_start"] == 0.0, p0
assert p0["silence"]["keeps"] == [[0.0, 5.0], [10.0, 20.0]], p0["silence"]
assert p0["expected_duration"] == 17.5, p0
p01 = json.load(open("planG01.json"))
# 複数指定時は「登壇者のうち最初に5秒以上話す人」の頭 = 登壇者の 5.0
assert p01["presenters"] == [0, 1] and p01["trim_start"] == 5.0, p01
print("OK   G plan JSON (presenters / trim_start)")
PY
$E render --plan planG0.json -o outG.mp4 > /dev/null 2>&1
check "$(dur outG.mp4)" 17.5 "G render (presenters=[0])"

echo "== Case H: カット候補の無効化 (disabled_cuts で無音を残す) =="
cat > cfgH.json <<'J'
{"silence_cut": true, "silence_sec": 3.0, "intro_sec": 2, "outro_sec": 2,
 "dissolve": 0.5, "disabled_cuts": [[5, 10]]}
J
$E plan --video sil.mp4 --thumb "$OLDPWD/$THUMB" --config cfgH.json \
  --out planH.json > /dev/null
python3 - <<'PY'
import json
p = json.load(open("planH.json"))
c = p["silence"]["candidates"]
assert len(c) == 1 and c[0]["enabled"] is False, c
assert p["silence"]["cuts"] == [] and p["silence"]["keeps"] == [[0.0, 20.0]], p["silence"]
assert p["expected_duration"] == 23.0, p
print("OK   H plan JSON (candidates / disabled_cuts)")
PY
$E render --plan planH.json -o outH.mp4 > /dev/null 2>&1
check "$(dur outH.mp4)" 23.0 "H render (silence kept)"

echo "REGRESSION OK"
