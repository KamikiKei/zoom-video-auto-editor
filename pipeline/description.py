"""YouTubeのタイトル・概要欄の生成 (基本設計 工程9)。

公開済み動画の実態 (docs/基本設計.md) に合わせ、タイトルは
「松尾研LLMコミュニティ + 隅付き括弧で Paper&Hacks #回数 + 発表タイトル」
の形式、概要欄は「定型ヘッダー + connpassイベント概要ブロック +
定型フッター」で組み立てる。ここでは connpass 相当の情報を
meta dict から受ける。

TODO: 定型文の言い回しは実際のチャンネルの文面と最終調整すること。
イベント回 (特別回) はタイトル形式が異なるため、meta["kind"] == "event"
の場合は title_override が無ければ None を返し、人間確認に回す。
"""

# 隅付き括弧。ソースには chr() で書く
LB = chr(0x3010)
RB = chr(0x3011)

HEADER = (
    "本イベントは、原則毎週火曜日20:00から松尾・岩澤研究室 LLMコミュニティが"
    "主催する、論文や技術トピックを持ち寄って発表・議論するイベントです。"
)

FOOTER = (
    "■ 次回以降の参加申込はこちら\n"
    "https://linktr.ee/matsuolab_community\n"
    "\n"
    "※ 本動画での発表内容は、発表者個人の見解であり、所属組織の公式見解ではありません。\n"
    "キーワード: LLM, 生成AI, 論文紹介, Paper & Hacks, 松尾・岩澤研究室"
)

# connpassイベント概要ブロックの項目 (ラベル, metaのキー)
_BLOCK_FIELDS = [
    ("対象", "audience"),
    ("レベル", "level"),
    ("発表者", "presenter"),
    ("タイトル", "talk_title"),
    ("発表カテゴリ", "category"),
    ("イベント内容", "about"),
    ("扱う論文", "papers"),
    ("注意・連絡事項", "notes"),
]


def build_title(meta):
    """タイトルを組み立てる。形式が決められない場合は None (人間確認)。"""
    if meta.get("title_override"):
        return meta["title_override"]
    talk = meta.get("talk_title") or meta.get("title") or ""
    if meta.get("kind") == "event":
        return None  # 特別回は形式が異なるため人間が決める
    no = meta.get("session_no")
    if no and talk:
        return f"松尾研LLMコミュニティ{LB}Paper&Hacks #{no}{RB}{talk}"
    return talk or None


def build_description(meta):
    """概要欄を組み立てる。meta に無い項目は行ごと省略する。"""
    lines = [HEADER, ""]
    block = []
    for label, key in _BLOCK_FIELDS:
        val = meta.get(key)
        if val:
            block.append(f"{LB}{label}{RB}{val}")
    if block:
        lines += block + [""]
    lines.append(FOOTER)
    return "\n".join(lines)
