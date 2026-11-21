#!/usr/bin/env python3
"""AI検出 (llm_detect) とデフォルト保持ポリシー (edit._policy_candidates)
のテスト。APIキーは使わず、モックとフェイクHTTPで全て回す。

実行: python3 test/test_ai_detect.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import llm_detect  # noqa: E402
import edit as edit_mod  # noqa: E402
from transcript import build_result, parse_transcript  # noqa: E402

FAILED = []


def check(label, cond, extra=""):
    if cond:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label} {extra}")
        FAILED.append(label)


VTT = """WEBVTT

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
登壇者: あれ、聞こえますか。少々お待ちください。
"""


def test_mock_detection():
    cues = parse_transcript(_write_vtt(VTT))
    found = llm_detect.detect_ranges(cues, llm_detect.MockLLMClient())
    kinds = {(c["start"], c["kind"]) for c in found}
    check("mock: トラブル発話 (聞こえますか) が trouble 候補になる",
          (16.0, "trouble") in kinds, str(found))
    check("mock: 通常の発表発話は候補にならない",
          all(c["start"] != 3.0 for c in found), str(found))
    check("mock: 候補に source=llm と reason が付く",
          all(c["source"] == "llm" and c["reason"] for c in found))


def test_mock_fumbling():
    vtt = """WEBVTT

1
00:00:00.000 --> 00:00:06.000
登壇者: えーと、えーと、あのー、まず、えーと、話します。

2
00:00:06.000 --> 00:00:10.000
登壇者: すみません、言い直します。結論から話します。
"""
    cues = parse_transcript(_write_vtt(vtt))
    found = llm_detect.detect_ranges(cues, llm_detect.MockLLMClient())
    kinds = {c["kind"] for c in found}
    # 2キューとも fumbling で、隣接しているため1区間にマージされる
    check("mock: フィラー連発と言い直しが fumbling 候補になる",
          kinds == {"fumbling"} and len(found) == 1
          and found[0]["start"] == 0.0 and found[0]["end"] == 10.0, str(found))


def test_windowing_and_merge():
    # 12秒間隔 x 200キュー = 2400秒。窓 (600秒) は複数に分かれる
    cues = []
    for i in range(200):
        cues.append({"start": i * 12.0, "end": i * 12.0 + 5.0,
                     "speaker": "登壇者", "text": "発表の内容です。"})
    # 重なり領域 (複数窓にまたがる位置) にトラブル発話を1つ置く。
    # 同じキューが2つの窓で検出されても、候補は1つにマージされるはず
    cues[49]["text"] = "聞こえますか。"

    class CountingMock(llm_detect.MockLLMClient):
        def __init__(self):
            self.windows = 0

        def detect_window(self, w):
            self.windows += 1
            return super().detect_window(w)

    client = CountingMock()
    found = llm_detect.detect_ranges(cues, client, window_sec=600, overlap_sec=60)
    check("windowing: 長い transcript は複数窓に分割される",
          client.windows >= 3, f"windows={client.windows}")
    trouble = [c for c in found if c["kind"] == "trouble"]
    check("windowing: 重なり領域の検出がマージされ重複しない",
          len(trouble) == 1 and trouble[0]["start"] == 49 * 12.0
          and trouble[0]["end"] == 49 * 12.0 + 5.0, str(trouble))


def test_gemini_client_with_fake_http():
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            payload = {"candidates": [{"content": {"parts": [{"text": json.dumps([
                {"start_cue": 0, "end_cue": 0, "kind": "trouble",
                 "reason": "音声確認", "confidence": 0.9},
                {"start_cue": 1, "end_cue": 1, "kind": "unknown-kind",
                 "reason": "不正な種別", "confidence": 0.5},
                {"start_cue": 99, "end_cue": 99, "kind": "trouble",
                 "reason": "範囲外", "confidence": 0.5},
            ])}]}}]}
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.headers.get("X-goog-api-key")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        client = llm_detect.GeminiClient(api_key="test-key", model="gemini-test")
        cues = [{"start": 0.0, "end": 3.0, "speaker": "運営", "text": "聞こえますか"},
                {"start": 3.0, "end": 6.0, "speaker": "登壇者", "text": "発表です"}]
        found = llm_detect.detect_ranges(cues, client)
    finally:
        urllib.request.urlopen = orig
    check("gemini: エンドポイントとモデルがURLに入る",
          "generativelanguage.googleapis.com" in captured["url"]
          and "gemini-test" in captured["url"], captured.get("url", ""))
    check("gemini: APIキーがヘッダで送られる", captured.get("key") == "test-key")
    gen = captured["body"]["generationConfig"]
    check("gemini: structured output (JSONモード+スキーマ) を使う",
          gen.get("responseMimeType") == "application/json"
          and gen.get("responseSchema", {}).get("type") == "ARRAY")
    check("gemini: 不正な項目 (種別・範囲外) は検証で落ちる",
          len(found) == 1 and found[0]["kind"] == "trouble"
          and found[0]["start"] == 0.0, str(found))


def test_env_client_selection():
    env = dict(os.environ)
    try:
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_DETECT"):
            os.environ.pop(k, None)
        check("env: キー無し・非デモでは None (スキップ)",
              llm_detect.get_client_from_env() is None)
        check("env: デモではモックになる",
              isinstance(llm_detect.get_client_from_env(demo=True),
                         llm_detect.MockLLMClient))
        os.environ["LLM_DETECT"] = "mock"
        check("env: LLM_DETECT=mock でモック",
              isinstance(llm_detect.get_client_from_env(),
                         llm_detect.MockLLMClient))
        os.environ["LLM_DETECT"] = "off"
        check("env: LLM_DETECT=off で無効化",
              llm_detect.get_client_from_env() is None)
        os.environ.pop("LLM_DETECT")
        os.environ["GEMINI_API_KEY"] = "dummy"
        check("env: GEMINI_API_KEY があれば GeminiClient",
              isinstance(llm_detect.get_client_from_env(),
                         llm_detect.GeminiClient))
    finally:
        os.environ.clear()
        os.environ.update(env)


def _write_vtt(text):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".vtt", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return Path(f.name)


def test_policy_candidates():
    cues = parse_transcript(_write_vtt(VTT))
    result = build_result(cues)
    check("policy: 登壇者推定は紹介パターンから登壇者 (index 1)",
          result.get("presenter_guess") == 1, str(result.get("presenter_guess")))
    cands = edit_mod._policy_candidates(result, cues, [1], 3.0, 20.0)
    by_src = {c["source"]: c for c in cands}
    check("policy: 運営の区間が source=ops の候補になる",
          "ops" in by_src and by_src["ops"]["start"] == 10.0
          and by_src["ops"]["end"] == 13.0, str(cands))
    check("policy: 参加者は source=other の候補になる",
          "other" in by_src and by_src["other"]["start"] == 13.0, str(cands))
    check("policy: 候補に reason が付く",
          all(c["reason"] for c in cands))
    check("policy: 登壇者が未確定なら候補は出ない",
          edit_mod._policy_candidates(result, cues, [], 0.0, 20.0) == [])


def _make_video(work: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
         "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(work / "plain.mp4")],
        check=True)


def test_compute_plan_integration():
    work = Path(tempfile.mkdtemp())
    _make_video(work)
    vtt = _write_vtt(VTT)
    thumb = BASE / "test" / "thumb.png"
    cfg = {"intro_sec": 2, "outro_sec": 2, "dissolve": 0.5}

    plan = edit_mod.compute_plan(work / "plain.mp4", cfg, transcript_path=vtt,
                                 thumb=thumb,
                                 llm_client=llm_detect.MockLLMClient())
    srcs = {c["source"] for c in plan["silence"]["candidates"]}
    check("plan: ops / other / llm の候補が最初から入る",
          {"ops", "other", "llm"} <= srcs, str(srcs))
    check("plan: llm_detection が ok で件数つき",
          plan["llm_detection"]["status"] == "ok"
          and plan["llm_detection"]["count"] >= 1, str(plan["llm_detection"]))
    check("plan: 保持は登壇者の発表区間のみ",
          plan["silence"]["keeps"] == [[3.0, 10.0]], str(plan["silence"]["keeps"]))
    check("plan: 想定尺は保持7秒ベース (2+7+2-1.0=10.0)",
          plan["expected_duration"] == 10.0, str(plan["expected_duration"]))
    check("plan: keep_policy が記録される",
          plan["keep_policy"] == {"keep_presenters_only": True, "applied": True},
          str(plan["keep_policy"]))

    # 候補を無効化すると保持に戻る (エディタのタップ切替 = disabled_cuts)
    cfg2 = {**cfg, "disabled_cuts": [[10, 13], [16, 20]]}
    plan2 = edit_mod.compute_plan(work / "plain.mp4", cfg2, transcript_path=vtt,
                                  thumb=thumb,
                                  llm_client=llm_detect.MockLLMClient())
    dis = {c["source"] for c in plan2["silence"]["candidates"] if not c["enabled"]}
    check("plan: disabled_cuts で ops と llm の候補が無効化される",
          {"ops", "llm"} <= dis, str(plan2["silence"]["candidates"]))
    check("plan: 無効化した区間は保持に戻る",
          plan2["silence"]["keeps"] == [[3.0, 13.0], [16.0, 20.0]],
          str(plan2["silence"]["keeps"]))

    # ポリシー無効化フラグ
    cfg3 = {**cfg, "keep_presenters_only": False}
    plan3 = edit_mod.compute_plan(work / "plain.mp4", cfg3, transcript_path=vtt,
                                  thumb=thumb)
    srcs3 = {c["source"] for c in plan3["silence"]["candidates"]}
    check("plan: keep_presenters_only=False でポリシー候補は出ない",
          not ({"ops", "other"} & srcs3), str(srcs3))
    check("plan: クライアント未指定なら llm_detection は skipped",
          plan3["llm_detection"]["status"] == "skipped",
          str(plan3["llm_detection"]))
    check("plan: ポリシーOFFでは全区間が保持",
          plan3["silence"]["keeps"] == [[3.0, 20.0]],
          str(plan3["silence"]["keeps"]))

    # LLMが失敗しても plan は作れる
    class BrokenClient:
        name = "broken"

        def detect_window(self, cues):
            raise RuntimeError("API down")

    plan4 = edit_mod.compute_plan(work / "plain.mp4", cfg, transcript_path=vtt,
                                  thumb=thumb, llm_client=BrokenClient())
    check("plan: LLM失敗時は status=failed で plan は作られる",
          plan4["llm_detection"]["status"] == "failed"
          and "API down" in plan4["llm_detection"]["detail"],
          str(plan4["llm_detection"]))


def main():
    test_mock_detection()
    test_mock_fumbling()
    test_windowing_and_merge()
    test_gemini_client_with_fake_http()
    test_env_client_selection()
    test_policy_candidates()
    test_compute_plan_integration()
    if FAILED:
        print(f"\nAI DETECT TEST FAILED: {FAILED}")
        return 1
    print("\nAI DETECT TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
