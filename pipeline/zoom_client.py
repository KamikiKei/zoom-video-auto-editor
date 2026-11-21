"""Zoom録画の取得クライアント。

インターフェース (ZoomClient) の背後に実接続を差し替える設計。
現時点の実装:
  - MockZoomClient : ローカルの inbox ディレクトリを「Zoomクラウド」とみなす
  - ZoomS2SClient   : 実接続用スタブ (未実装。認証情報が揃ったら実装する)
"""
import json
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".mp4", ".mov", ".webm"}


@dataclass
class Recording:
    """1件の録画。download() 後はローカルパスが入る。"""
    id: str
    title: str = ""
    started_at: str = ""           # ISO 8601 (例: 2026-11-17T20:00:00+09:00)
    video: Path | None = None      # ローカル動画パス
    transcript: Path | None = None  # ローカルVTTパス (無ければ None)
    asset: Path | None = None      # OP/ED素材 (モック専用。下記注記参照)
    meta: dict = field(default_factory=dict)  # connpass相当のメタ情報

    # 注記: 実運用では OP/ED素材は Zoom 録画には含まれず、Canva 等から
    # 別工程で取得する (基本設計 工程3)。モックでは便宜上 inbox の
    # 録画フォルダに同梱された素材ファイルをそのまま使う。


class ZoomClient(ABC):
    @abstractmethod
    def list_recordings(self) -> list[Recording]:
        """取り込み候補の録画一覧を返す (ダウンロード前の状態でよい)。"""

    @abstractmethod
    def download(self, rec: Recording, dest_dir: Path) -> Recording:
        """録画ファイル一式を dest_dir に取得し、ローカルパス入りの
        Recording を返す。"""


class MockZoomClient(ZoomClient):
    """inbox/<recording_id>/ を1件の録画とみなすモック。

    フォルダ構成:
      inbox/<id>/video.mp4        録画本体 (必須。.mov でも可)
      inbox/<id>/transcript.vtt   Zoom文字起こし (任意)
      inbox/<id>/thumb.png 等     OP/ED素材 (任意。thumb.* を探す)
      inbox/<id>/meta.json        {title, started_at, presenter, session_no, ...}
    """

    def __init__(self, inbox: Path):
        self.inbox = Path(inbox)

    def list_recordings(self) -> list[Recording]:
        recs = []
        if not self.inbox.is_dir():
            return recs
        for d in sorted(self.inbox.iterdir()):
            if not d.is_dir():
                continue
            video = next((p for p in sorted(d.iterdir())
                          if p.suffix.lower() in VIDEO_EXTS
                          and p.name != "thumb.mp4"), None)
            if video is None:
                continue
            meta = {}
            meta_f = d / "meta.json"
            if meta_f.exists():
                meta = json.loads(meta_f.read_text(encoding="utf-8"))
            vtt = d / "transcript.vtt"
            asset = next((p for p in sorted(d.iterdir())
                          if p.stem == "thumb" and p.suffix.lower() in ASSET_EXTS), None)
            recs.append(Recording(
                id=d.name,
                title=meta.get("title", ""),
                started_at=meta.get("started_at", ""),
                video=video,
                transcript=vtt if vtt.exists() else None,
                asset=asset,
                meta=meta,
            ))
        return recs

    def download(self, rec: Recording, dest_dir: Path) -> Recording:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = Recording(id=rec.id, title=rec.title, started_at=rec.started_at,
                        meta=dict(rec.meta))
        out.video = dest_dir / "video.mp4"
        shutil.copy2(rec.video, out.video)
        if rec.transcript is not None:
            out.transcript = dest_dir / "transcript.vtt"
            shutil.copy2(rec.transcript, out.transcript)
        if rec.asset is not None:
            out.asset = dest_dir / rec.asset.name
            shutil.copy2(rec.asset, out.asset)
        return out


class ZoomS2SClient(ZoomClient):
    """Zoom Server-to-Server OAuth による実接続 (スタブ)。

    実装時に使う認証情報 (環境変数):
      ZOOM_ACCOUNT_ID    : ZoomアカウントID
      ZOOM_CLIENT_ID     : S2S OAuthアプリの Client ID
      ZOOM_CLIENT_SECRET : S2S OAuthアプリの Client Secret

    利用エンドポイント:
      アクセストークン取得:
        POST https://zoom.us/oauth/token
          ?grant_type=account_credentials&account_id={ZOOM_ACCOUNT_ID}
          Authorization: Basic base64({ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET})
      録画一覧:
        GET https://api.zoom.us/v2/accounts/me/recordings?from=YYYY-MM-DD&to=YYYY-MM-DD
        (または GET /v2/users/{userId}/recordings)
        必要スコープ: cloud_recording:read:list_account_recordings:admin 等
      録画ファイル取得:
        レスポンス meetings[].recording_files[] の download_url に
        ?access_token={token} を付けて GET。
        file_type == "TRANSCRIPT" のものが VTT (文字起こし)。
        recording_complete Webhook を併用すると検知が速くなる (任意)。

    注意: S2Sアプリの作成には Zoom アカウント所有者/管理者権限が必要
    (docs/基本設計.md の未確定事項を参照)。
    """

    def __init__(self):
        self.account_id = os.environ.get("ZOOM_ACCOUNT_ID")
        self.client_id = os.environ.get("ZOOM_CLIENT_ID")
        self.client_secret = os.environ.get("ZOOM_CLIENT_SECRET")

    def list_recordings(self) -> list[Recording]:
        raise NotImplementedError(
            "ZoomS2SClient は未実装です。ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / "
            "ZOOM_CLIENT_SECRET を設定し、docstring のエンドポイントで実装してください")

    def download(self, rec: Recording, dest_dir: Path) -> Recording:
        raise NotImplementedError("ZoomS2SClient は未実装です")


def get_zoom_client(inbox: Path) -> ZoomClient:
    """環境変数 ZOOM_CLIENT_ID があれば実接続、無ければモックを返す。
    (実接続が未実装の間は、認証情報を設定すると NotImplementedError になる)"""
    if os.environ.get("ZOOM_CLIENT_ID"):
        return ZoomS2SClient()
    return MockZoomClient(inbox)
