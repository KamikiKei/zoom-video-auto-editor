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
# 注: Case G は「開始点の提案ロジック」単体の検証。デフォルト保持ポリシー
# (登壇者以外をカット候補にする) が入ると presenters=[0] のときに登壇者の
# 区間まで候補になり期待値が変わるため、ここではポリシーを無効化する。
# ポリシー自体の検証は Case K と test/test_ai_detect.py で行う。
$E plan --video sil.mp4 --transcript sample.vtt --thumb "$OLDPWD/$THUMB" \
  --silence-cut --silence-sec 3 --intro-sec 2 --outro-sec 2 --dissolve 0.5 \
  --presenters 0 --no-keep-presenters-only --out planG0.json > /dev/null
$E plan --video sil.mp4 --transcript sample.vtt --thumb "$OLDPWD/$THUMB" \
  --silence-cut --silence-sec 3 --intro-sec 2 --outro-sec 2 --dissolve 0.5 \
  --presenters 0,1 --no-keep-presenters-only --out planG01.json > /dev/null
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

echo "== Case I: ユーザーカット (保持区間の一部を手動で切る) =="
cat > cfgI.json <<'J'
{"intro_sec": 2, "outro_sec": 2, "dissolve": 0.5, "user_cuts": [[5, 10]]}
J
$E plan --video plain.mp4 --thumb "$OLDPWD/$THUMB" --config cfgI.json \
  --out planI.json > /dev/null
python3 - <<'PY'
import json
p = json.load(open("planI.json"))
c = p["silence"]["candidates"]
assert len(c) == 1 and c[0]["source"] == "user" and c[0]["enabled"] is True, c
assert p["silence"]["user_cuts"] == [[5.0, 10.0]], p["silence"]
assert p["silence"]["keeps"] == [[0.0, 5.0], [10.0, 20.0]], p["silence"]
# 保持15s - つなぎ0.5 + 2 + 2 - 0.5*2 = 17.5
assert p["expected_duration"] == 17.5, p
print("OK   I plan JSON (user_cuts / candidates source)")
PY
$E render --plan planI.json -o outI.mp4 > /dev/null 2>&1
check "$(dur outI.mp4)" 17.5 "I render (user cut via plan)"
$E plain.mp4 "$OLDPWD/$THUMB" -o outI2.mp4 --config cfgI.json > /dev/null 2>&1
check "$(dur outI2.mp4)" 17.5 "I build_and_run (user cut via cfg)"

echo "== Case J: ユーザーカット + 無音カット併用 =="
cat > cfgJ.json <<'J'
{"silence_cut": true, "silence_sec": 3.0, "intro_sec": 2, "outro_sec": 2,
 "dissolve": 0.5, "user_cuts": [[12, 14]]}
J
$E plan --video sil.mp4 --thumb "$OLDPWD/$THUMB" --config cfgJ.json \
  --out planJ.json > /dev/null
python3 - <<'PY'
import json
p = json.load(open("planJ.json"))
srcs = sorted(c["source"] for c in p["silence"]["candidates"])
assert srcs == ["silence", "user"], srcs
# 無音 [5,10] と手動 [12,14] が両方 keeps から引かれる
assert p["silence"]["keeps"] == [[0.0, 5.0], [10.0, 12.0], [14.0, 20.0]], p["silence"]
print("OK   J plan JSON (silence + user cuts)")
PY
$E render --plan planJ.json -o outJ.mp4 > /dev/null 2>&1
# 保持13s - つなぎ0.5*2 + 2 + 2 - 0.5*2 = 15.0
check "$(dur outJ.mp4)" 15.0 "J render (silence + user)"

echo "== Case K: デフォルト保持ポリシー (登壇者以外は最初からカット候補) =="
# rich.vtt: 運営の紹介 (0-3) -> 登壇者 (3-10) -> 運営の連絡 (10-13)
#           -> 参加者の質問 (13-16) -> 登壇者 (16-20)
cat > rich.vtt <<'VTT'
WEBVTT

1
00:00:00.000 --> 00:00:03.000
運営: それでは本日の登壇者をご紹介します。

2
00:00:03.000 --> 00:00:10.000
登壇者: はい、よろしくお願いします。本日はRAGの評価について話します。

3
00:00:10.000 --> 00:00:13.000
運営: ここで次回日程の連絡です。

4
00:00:13.000 --> 00:00:16.000
参加者A: すみません、質問があります。

5
00:00:16.000 --> 00:00:20.000
登壇者: では最後にまとめます。
VTT
$E plan --video plain.mp4 --transcript rich.vtt --thumb "$OLDPWD/$THUMB" \
  --intro-sec 2 --outro-sec 2 --dissolve 0.5 --out planK.json > /dev/null
python3 - <<'PY'
import json
p = json.load(open("planK.json"))
cands = p["silence"]["candidates"]
by_src = {c["source"]: c for c in cands}
assert by_src["ops"]["start"] == 10.0 and by_src["ops"]["end"] == 13.0, cands
assert by_src["other"]["start"] == 13.0 and by_src["other"]["end"] == 16.0, cands
assert all(c["enabled"] for c in cands), cands
assert all(c["reason"] for c in cands), cands
# 登壇者の発表区間 [3,10] だけが残る (開始点も登壇者の頭 3.0)
assert p["trim_start"] == 3.0, p
assert p["silence"]["keeps"] == [[3.0, 10.0], [16.0, 20.0]], p["silence"]
assert p["keep_policy"] == {"keep_presenters_only": True, "applied": True}, p
assert p["llm_detection"]["status"] == "skipped", p["llm_detection"]
# 保持11s - つなぎ0.5 + 2 + 2 - 0.5*2 = 13.5
assert p["expected_duration"] == 13.5, p
print("OK   K plan JSON (policy candidates ops/other)")
PY
$E render --plan planK.json -o outK.mp4 > /dev/null 2>&1
check "$(dur outK.mp4)" 13.5 "K render (policy)"

echo "== Case L: 強制レガシー (2パス) でも無音以外のカットが効く =="
# 回帰防止: 以前は _render_legacy が無音カット有効時のみ保持区間を適用して
# いたため、ユーザー/ポリシー/LLM カットだけの plan を 2パスで描くと
# カットが全部落ちていた。one_pass=False で同じことを確認する。
cat > cfgL.json <<'JSON'
{"trim_start": 3, "end": 20, "intro_sec": 2, "outro_sec": 2, "dissolve": 0.5,
 "silence_cut": false, "user_cuts": [[13, 20]], "one_pass": false}
JSON
$E plain.mp4 "$OLDPWD/$THUMB" -o outL.mp4 --config cfgL.json > /dev/null 2>&1
# 保持 [3,13] の10s + 2 + 2 - 0.5*2 = 13.0
check "$(dur outL.mp4)" 13.0 "L legacy fallback (user cut, silence off)"

checktol() { # $1=実測 $2=期待 $3=ラベル $4=許容誤差
  python3 -c "import sys; sys.exit(0 if abs(float('$1') - float('$2')) <= float('$4') else 1)" \
    && echo "OK   $3: $1 (expect $2)" || { echo "FAIL $3: $1 (expect $2)"; exit 1; }
}

echo "== Case M: 保持区間が多い plan の分割方式 + 動画素材バリエーション =="
# 回帰防止: 保持区間を1つのグラフで全部 xfade 接続すると、後ろの区間の
# フレームがキューに溜まり続けてメモリを使い切り、ffmpeg が kill される
# (実素材 1080p・50区間・本編76分で発生)。分割方式 (ボディ断片 +
# トランジション断片を個別に書き出し、concat で連結。断片の音声は
# AAC プライミング漏れを避けるため PCM で書き、連結時に再エンコード)
# で有界メモリ・正確な尺に収まることを確認する。
# big: 150秒、音声は 音1.5s / 無音1.0s の繰り返し -> 保持区間が約60個できる
ffmpeg -y -v error -f lavfi -i testsrc2=size=320x180:rate=30:duration=150 \
  -f lavfi -i "aevalsrc='if(lt(mod(t,2.5),1.5),0.3*sin(2*PI*440*t),0)':s=48000:d=150" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac big.mp4
# 素材バリエーション (各5秒): 音声あり同スペック / 音声なし / fps・解像度違い / その両方
ffmpeg -y -v error -f lavfi -i testsrc=size=640x360:rate=30:duration=5 \
  -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=5" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac assetM_audio.mp4
ffmpeg -y -v error -f lavfi -i testsrc=size=640x360:rate=30:duration=5 \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p assetM_noaudio.mp4
ffmpeg -y -v error -f lavfi -i testsrc=size=320x240:rate=15:duration=5 \
  -f lavfi -i "sine=frequency=880:sample_rate=44100:duration=5" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac assetM_diff.mp4
ffmpeg -y -v error -f lavfi -i testsrc=size=320x240:rate=15:duration=5 \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p assetM_diff_noaudio.mp4
$E plan --video big.mp4 --thumb assetM_audio.mp4 --silence-cut --silence-sec 0.9 \
  --dissolve 0.5 --out planM.json > planM.log
python3 - <<'PY'
import json
p = json.load(open("planM.json"))
keeps = p["silence"]["keeps"]
assert len(keeps) >= 40, f"keeps={len(keeps)}"
open("expectedM.txt", "w").write(str(p["expected_duration"]))
print(f"OK   M plan (keeps={len(keeps)}, expected={p['expected_duration']})")
PY
for v in audio noaudio diff diff_noaudio; do
  $E render --plan planM.json --thumb assetM_$v.mp4 -o outM_$v.mp4 > renderM_$v.log 2>&1
  grep -q "分割方式" renderM_$v.log || { echo "FAIL M render ($v): 分割方式が使われていない"; cat renderM_$v.log; exit 1; }
  checktol "$(dur outM_$v.mp4)" "$(cat expectedM.txt)" "M render ($v, smart)" 0.6
done

echo "== Case N: 1パス方式での動画素材バリエーション =="
# 小規模 plan の 1パスでも、音声なし素材 / fps・解像度違い素材が扱えること
ffmpeg -y -v error -f lavfi -i testsrc=size=640x360:rate=30:duration=2 \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p assetN_noaudio.mp4
ffmpeg -y -v error -f lavfi -i testsrc=size=320x240:rate=15:duration=2 \
  -f lavfi -i "sine=frequency=880:sample_rate=44100:duration=2" \
  -c:v libx264 -crf 23 -preset veryfast -pix_fmt yuv420p -c:a aac assetN_diff.mp4
$E plain.mp4 assetN_noaudio.mp4 -o outN1.mp4 --config cfgC.json > n1.log 2>&1
grep -q "1パス" n1.log || { echo "FAIL N: 1パスで書けていない"; cat n1.log; exit 1; }
check "$(dur outN1.mp4)" 11.0 "N 1pass (no-audio asset)"
$E plain.mp4 assetN_diff.mp4 -o outN2.mp4 --config cfgC.json > n2.log 2>&1
grep -q "1パス" n2.log || { echo "FAIL N: 1パスで書けていない"; cat n2.log; exit 1; }
check "$(dur outN2.mp4)" 11.0 "N 1pass (diff fps/size asset)"

echo "== AI検出・ポリシーの単体テスト (モック/フェイクHTTP) =="
python3 "$OLDPWD/test/test_ai_detect.py" > ai_detect.log 2>&1 \
  && echo "OK   test_ai_detect.py" || { cat ai_detect.log; exit 1; }

echo "REGRESSION OK"
