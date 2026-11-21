#!/usr/bin/env python3
"""
transcript (Zoom VTT のキュー列) から「カット候補にすべき区間」を
LLM API で検出するモジュール。

検出するのは次の3種類 (kind):
  - trouble : 音声・画面・接続などのトラブル対応・確認のやりとり
  - fumbling: 言い淀み、フィラーの連発、言い直し・自己修正が目立つ区間
  - offtopic: 発表本題と関係ない段取り・連絡・雑談

あくまで「候補」を出すだけの工程で、最終判断は人間がエディタで行う
(基本設計のヒューマンインザループ)。そのため精度差は二の次とし、
実クライアントは Google Gemini API (AI Studio、無料枠) を使う。
選定理由の詳細は docs/AI検出.md を参照。

使い方:
  client = get_client_from_env()          # キーが無ければ None
  candidates = detect_ranges(cues, client)  # cues は transcript.parse_transcript の戻り

環境変数:
  GEMINI_API_KEY / GOOGLE_API_KEY : Gemini API キー
  GEMINI_MODEL                    : モデルID (既定 gemini-2.5-flash)
  LLM_DETECT                      : "mock" でモック強制、"off" で無効化

依存は Python 標準ライブラリのみ (urllib)。
"""
import json
import os
import re
import urllib.request

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
KINDS = ("trouble", "fumbling", "offtopic")

SYSTEM_PROMPT = """あなたはオンラインセミナーの動画編集を支援するAIです。
登壇者の発表動画の文字起こし (キュー列) を読み、カット候補にすべき区間を検出してください。"""

USER_TEMPLATE = """以下はセミナーの文字起こしです。各行が1キューで、[番号] 開始-終了(秒) 話者: テキスト の形式です。

{cues}

次の種類に当てはまる区間だけを、カット候補として返してください:
- trouble: 音声・画面・接続・配信などのトラブル対応や、聞こえるか/見えるかの確認のやりとり
- fumbling: 言い淀み、フィラー (えーと、あのー等) の連発、言い直し・自己修正が目立つ区間
- offtopic: 発表の本題と関係ない段取り・連絡・雑談

守ること:
- 発表内容そのもの、内容を考えるための短い間、通常の言い回しは候補にしない
- 迷ったら候補にしない (明らかなトラブル対応は拾う)
- 区間はキュー番号 (start_cue, end_cue) で指定し、理由を日本語で短く書く
- confidence は 0.0〜1.0 で、確信度が高いほど大きくする

出力は指定されたJSON形式のみとし、説明文は付けないでください。"""

# Gemini の structured output (responseSchema) で JSON 形状を強制する
RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start_cue": {"type": "INTEGER"},
            "end_cue": {"type": "INTEGER"},
            "kind": {"type": "STRING", "enum": list(KINDS)},
            "reason": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["start_cue", "end_cue", "kind", "reason"],
    },
}


def format_cues(cues) -> str:
    """キュー列をプロンプト用の番号付きテキストにする。
    cues の各要素は {"index", "start", "end", "speaker", "text"} を持つこと。"""
    lines = []
    for c in cues:
        sp = c.get("speaker") or "話者不明"
        lines.append(f"[{c['index']}] {c['start']:.1f}-{c['end']:.1f} {sp}: {c['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 実クライアント: Google Gemini API (AI Studio)
# ---------------------------------------------------------------------------
class GeminiClient:
    """Gemini API を urllib で直接叩くクライアント。"""

    name = "gemini"

    def __init__(self, api_key=None, model=None, timeout=180):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") \
            or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY (または GOOGLE_API_KEY) が未設定です")
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def detect_window(self, cues):
        """1つの時間窓のキュー列を受け取り、生の検出項目リストを返す。
        検証は detect_ranges 側で行うので、ここでは JSON をそのまま返す。"""
        prompt = USER_TEMPLATE.format(cues=format_cues(cues))
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                # structured output: JSON モード + スキーマで形状を強制
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }
        url = f"{API_BASE}/models/{self.model}:generateContent"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = ""
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part:
                    text += part["text"]
        if not text:
            raise RuntimeError("Gemini API の応答にテキストがありません")
        return _extract_json_array(text)


def _extract_json_array(text: str):
    """応答テキストから JSON 配列を取り出す (コードフェンス等の混入に耐える)。"""
    text = text.strip()
    try:
        v = json.loads(text)
        if isinstance(v, list):
            return v
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        v = json.loads(m.group(0))
        if isinstance(v, list):
            return v
    raise ValueError("Gemini API の応答を JSON 配列として解釈できませんでした")


# ---------------------------------------------------------------------------
# モッククライアント (テスト・デモ用、決定的)
# ---------------------------------------------------------------------------
class MockLLMClient:
    """キーワードベースの決定的な疑似検出。APIキーなしでテストを通すため。"""

    name = "mock"

    _TROUBLE = ("聞こえますか", "聞こえていますか", "見えますか", "見えていますか",
                "画面が見え", "画面が映", "映らない", "音声が", "音が出",
                "接続が", "つながら", "少々お待ち", "お待ちください",
                "ミュート", "トラブル", "再接続")
    _FUMBLE = ("言い直し", "言い直します", "えーと", "えっと", "あのー", "そのー")
    _OFFTOPIC = ("余談", "雑談ですが", "段取りの確認")
    _FILLERS = ("えー", "あー", "えっと", "あのー", "えーと")

    def detect_window(self, cues):
        items = []
        for c in cues:
            text = c.get("text") or ""
            hit = self._classify(text)
            if hit is None:
                continue
            kind, reason, conf = hit
            items.append({"start_cue": c["index"], "end_cue": c["index"],
                          "kind": kind, "reason": reason, "confidence": conf})
        return items

    def _classify(self, text):
        for w in self._TROUBLE:
            if w in text:
                return ("trouble", f"「{w}」を含むトラブル対応の発話 (モック検出)", 0.85)
        n_filler = sum(text.count(f) for f in self._FILLERS)
        for w in self._FUMBLE:
            if w in text:
                return ("fumbling", f"「{w}」を含む言い淀み・言い直し (モック検出)", 0.7)
        if n_filler >= 3:
            return ("fumbling", "フィラーが連発している (モック検出)", 0.6)
        for w in self._OFFTOPIC:
            if w in text:
                return ("offtopic", f"「{w}」を含む本題外の発話 (モック検出)", 0.6)
        return None


# ---------------------------------------------------------------------------
# 環境変数からのクライアント選択
# ---------------------------------------------------------------------------
def get_client_from_env(demo: bool = False):
    """環境変数からクライアントを選ぶ。使えない場合は None (=検出スキップ)。

    - LLM_DETECT=mock → MockLLMClient (テスト・デモ用)
    - LLM_DETECT=off  → None
    - GEMINI_API_KEY / GOOGLE_API_KEY があれば GeminiClient
    - デモモードでキーが無い場合は MockLLMClient (候補が付くところまで通すため)
    """
    mode = os.environ.get("LLM_DETECT", "").strip().lower()
    if mode == "mock":
        return MockLLMClient()
    if mode == "off":
        return None
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return GeminiClient(api_key=key)
    if demo:
        return MockLLMClient()
    return None


# ---------------------------------------------------------------------------
# 時間窓分割 + 検証 + マージ
# ---------------------------------------------------------------------------
def _windows(cues, window_sec: float, overlap_sec: float):
    """キューの index 範囲 [(start_idx, end_idx), ...] を返す。
    各窓は window_sec 秒を目安に切り、overlap_sec 秒ぶん重ねる。"""
    n = len(cues)
    wins = []
    i = 0
    while i < n:
        start_t = cues[i]["start"]
        j = i
        while j < n and cues[j]["start"] < start_t + window_sec:
            j += 1
        wins.append((i, j))
        if j >= n:
            break
        limit = start_t + window_sec - overlap_sec
        k = i + 1
        while k < n and cues[k]["start"] < limit:
            k += 1
        i = max(k, i + 1)
    return wins


def _validate_items(items, by_index):
    """生の検出項目を検証し、候補 dict のリストにする。不正な項目は捨てる。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        if kind not in KINDS:
            continue
        try:
            si, ei = int(it["start_cue"]), int(it["end_cue"])
        except (KeyError, TypeError, ValueError):
            continue
        if si not in by_index or ei not in by_index or ei < si:
            continue
        try:
            conf = float(it.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = min(1.0, max(0.0, conf))
        out.append({
            "start": round(float(by_index[si]["start"]), 3),
            "end": round(float(by_index[ei]["end"]), 3),
            "kind": kind,
            "source": "llm",
            "reason": str(it.get("reason") or "")[:200],
            "confidence": conf,
        })
    return out


def _merge(cands):
    """同種で重なる/隣接する候補をまとめる (窓の重なりによる重複排除)。"""
    merged = []
    for c in sorted(cands, key=lambda x: (x["start"], x["end"])):
        if (merged and c["kind"] == merged[-1]["kind"]
                and c["start"] - merged[-1]["end"] < 1.0):
            m = merged[-1]
            m["end"] = max(m["end"], c["end"])
            if c["confidence"] > m["confidence"]:
                m["confidence"] = c["confidence"]
                m["reason"] = c["reason"]
        else:
            merged.append(dict(c))
    return merged


def detect_ranges(cues, client, window_sec: float = 600.0,
                  overlap_sec: float = 60.0):
    """キュー列全体に対して検出し、カット候補 dict のリストを返す。

    cues は transcript.parse_transcript の戻り (start/end/speaker/text)。
    戻り値の各要素は {"start", "end", "kind", "source": "llm",
    "reason", "confidence"} で、enabled は呼び出し側が付ける。
    クライアントが例外を投げたらそのまま伝播する (呼び出し側で status 化)。
    """
    indexed = [dict(c, index=i) for i, c in enumerate(cues)]
    by_index = {c["index"]: c for c in indexed}
    found = []
    for a, b in _windows(indexed, window_sec, overlap_sec):
        items = client.detect_window(indexed[a:b])
        found.extend(_validate_items(items, by_index))
    return _merge(found)
