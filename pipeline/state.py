"""パイプラインの状態永続化 (JSON)。

録画IDごとに処理状況を持ち、処理済み録画の再処理を防ぐ。
ステータス遷移:
  (新規) -> awaiting_review   ジョブ作成＋確認依頼①の送信済み
         -> done              書き出し→アップロード→確認依頼②まで完了
         -> error             失敗 (エラー通知済み。再実行で拾い直せる)
"""
import json
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"recordings": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, rec_id: str) -> dict | None:
        return self.data["recordings"].get(rec_id)

    def put(self, rec_id: str, **fields) -> dict:
        rec = self.data["recordings"].setdefault(rec_id, {})
        rec.update(fields)
        self.save()
        return rec

    def awaiting_review(self) -> list[tuple[str, dict]]:
        return [(rid, r) for rid, r in self.data["recordings"].items()
                if r.get("status") == "awaiting_review"]
