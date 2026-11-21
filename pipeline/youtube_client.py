"""YouTubeアップロードクライアント。

現時点の実装:
  - MockYouTubeClient    : outbox にコピーしてダミーURLを返す
  - YouTubeDataAPIClient : 実接続用スタブ (未実装。OAuth認可が前提)
"""
import hashlib
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UploadResult:
    video_id: str
    url: str


class YouTubeClient(ABC):
    @abstractmethod
    def upload_private(self, video: Path, title: str, description: str,
                       thumbnail: Path | None = None) -> UploadResult:
        """動画を「非公開」でアップロードし、結果を返す。
        公開への切り替えは人間が行う (基本設計 工程12)。"""


class MockYouTubeClient(YouTubeClient):
    """outbox/ にファイルをコピーし、実在しないダミーURLを返すモック。"""

    def __init__(self, outbox: Path):
        self.outbox = Path(outbox)

    def upload_private(self, video: Path, title: str, description: str,
                       thumbnail: Path | None = None) -> UploadResult:
        self.outbox.mkdir(parents=True, exist_ok=True)
        video = Path(video)
        digest = hashlib.sha256(video.read_bytes()).hexdigest()[:11]
        video_id = f"mock{digest}"
        dest = self.outbox / f"{video_id}_{video.name}"
        shutil.copy2(video, dest)
        # 付けたタイトル・概要欄も確認できるよう横に残す
        (self.outbox / f"{video_id}_meta.txt").write_text(
            f"title: {title}\n\n{description}", encoding="utf-8")
        return UploadResult(
            video_id=video_id,
            url=f"https://mock-youtube.example.invalid/watch?v={video_id}")


class YouTubeDataAPIClient(YouTubeClient):
    """YouTube Data API v3 による実接続 (スタブ)。

    TODO (実装時の前提):
      - Google Cloud プロジェクトで YouTube Data API v3 を有効化し、
        OAuth 2.0 クライアント (デスクトップ型) を作成する
      - チャンネル管理者アカウントで初回認可し、リフレッシュトークンを
        安全な場所に保管する (OAuth同意画面を「本番環境」にしておかないと
        リフレッシュトークンが7日で失効する)
      - 環境変数:
          YOUTUBE_CLIENT_SECRETS : OAuthクライアントJSONのパス
          YOUTUBE_TOKEN_FILE      : 認可済みトークンの保存先パス
      - アップロード: youtube.videos().insert(part="snippet,status",
        body={snippet: {title, description}, status: {privacyStatus: "private"}})
        サムネイル: youtube.thumbnails().set(videoId, media_body=...)
      - クォータ: 既定 10,000 units/日。アップロード1回 1,600 units 程度
        なので月4本なら十分収まる
    """

    def __init__(self):
        self.client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS")
        self.token_file = os.environ.get("YOUTUBE_TOKEN_FILE")

    def upload_private(self, video: Path, title: str, description: str,
                       thumbnail: Path | None = None) -> UploadResult:
        raise NotImplementedError(
            "YouTubeDataAPIClient は未実装です。OAuth認可のうえ docstring の "
            "TODO に沿って実装してください")


def get_youtube_client(outbox: Path) -> YouTubeClient:
    """環境変数 YOUTUBE_CLIENT_SECRETS があれば実接続、無ければモック。"""
    if os.environ.get("YOUTUBE_CLIENT_SECRETS"):
        return YouTubeDataAPIClient()
    return MockYouTubeClient(outbox)
