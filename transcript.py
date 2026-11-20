#!/usr/bin/env python3
"""
Zoom公式の文字起こし (VTT) から話者区間を作る。

Zoomのクラウド録画トランスクリプトは話者名ラベル付きで出力されるため、
音声からの話者分離より圧倒的に正確。アカウント名と紐づいているので、
「誰が運営で誰が登壇者か」も名前で判断できる。

やること:
  1. VTTをパースして (開始, 終了, 話者名, テキスト) のキュー列にする
  2. 連続する同一話者のキューを区間にまとめる
  3. 運営の「ご紹介します」等の紹介発言の直後に話し始めた人を登壇者候補とする
  4. diarize.diarize() と同じ形式の結果 dict を返す
"""
import re
from pathlib import Path

_TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_VTAG = re.compile(r"<v(?:\s+[^>]*)?>(.*?)</v>", re.S)
_NAME_PREFIX = re.compile(r"^([^:：]{1,40})[:：]\s*(.*)$", re.S)
_INTRO_PATTERNS = [
    re.compile(r"ご紹介|紹介(します|いたします|させて)|お迎え|ご登壇|登壇者"),
    re.compile(r"(それでは|では)[^。]{0,30}お願い(します|いたします)"),
]


def _to_sec(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _split_speaker(text):
    """キューのテキストから (話者名, 本文) を取り出す。話者名がなければ (None, text)"""
    text = text.strip()
    vm = _VTAG.search(text)
    if vm:
        # <v Name>text</v> 形式: タグ属性側に名前が入る場合と本文のみの場合がある
        tag = re.search(r"<v\s+([^>]+)>", text)
        name = tag.group(1).strip() if tag else None
        body = vm.group(1).strip()
        return (name or None), body
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, ""
    m = _NAME_PREFIX.match(lines[0])
    if m:
        name = m.group(1).strip()
        # 文の途中のコロンを話者名と誤認しない: 名前側に文末記号がなく短い場合のみ採用
        if name and not re.search(r"[。、！？!?]", name):
            rest = m.group(2).strip()
            body = "\n".join(([rest] if rest else []) + lines[1:])
            return name, body
    return None, text


def parse_transcript(path: Path):
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    blocks = re.split(r"\n\s*\n", raw)
    for block in blocks:
        tm = _TS.search(block)
        if not tm:
            continue
        start = _to_sec(tm.group(1), tm.group(2), tm.group(3), tm.group(4))
        end = _to_sec(tm.group(5), tm.group(6), tm.group(7), tm.group(8))
        text_part = block[tm.end():].strip()
        # キュー番号行が残ることがあるので除去
        text_part = re.sub(r"^\d+\s*\n", "", text_part)
        name, body = _split_speaker(text_part)
        cues.append({"start": start, "end": end, "speaker": name, "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues


def _detect_presenter(cues):
    """紹介発言の直後に話し始めた別話者を登壇者候補として返す (名前 or None)"""
    for i, cue in enumerate(cues):
        if cue["speaker"] is None:
            continue
        if not any(p.search(cue["text"]) for p in _INTRO_PATTERNS):
            continue
        for nxt in cues[i + 1:]:
            if nxt["start"] - cue["end"] > 60:
                break
            if nxt["speaker"] and nxt["speaker"] != cue["speaker"]:
                return nxt["speaker"]
    return None


def build_result(cues):
    named = [c for c in cues if c["speaker"]]
    if not named:
        raise ValueError("文字起こしに話者名が含まれていません")
    names = {}
    for c in cues:
        if c["speaker"] and c["speaker"] not in names:
            names[c["speaker"]] = len(names)

    # 連続する同一話者のキューを区間にまとめる (間隔1.5秒未満はつなぐ)
    segments = []
    for c in cues:
        if c["speaker"] is None:
            continue
        idx = names[c["speaker"]]
        if segments and segments[-1][2] == idx and c["start"] - segments[-1][1] < 1.5:
            segments[-1][1] = max(segments[-1][1], c["end"])
        else:
            segments.append([c["start"], max(c["end"], c["start"] + 0.5), idx])
    if not segments:
        raise ValueError("文字起こしから区間を作れませんでした")

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from diarize import _summarize
    result = _summarize(segments)
    result["names"] = {str(v): k for k, v in names.items()}
    result["backend"] = "zoom-transcript"

    presenter_name = _detect_presenter(cues)
    if presenter_name is not None:
        pidx = names[presenter_name]
        result["presenter_guess"] = pidx
        result["main_speaker_guess"] = pidx
        # 提案は登壇者の最初の長い区間の頭に合わせ直す
        for a, b, lab in segments:
            if lab == pidx and (b - a) >= 5:
                result["suggested_trim_start"] = round(float(a), 1)
                break
    return result


def result_from_transcript(path: Path):
    return build_result(parse_transcript(path))
