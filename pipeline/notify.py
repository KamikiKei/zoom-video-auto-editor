"""Slack通知 (確認依頼1 / 2 とエラー通知)。

現時点の実装:
  - ConsoleNotifier : 標準出力に表示 (デモ・開発用)
  - WebhookNotifier : 環境変数 SLACK_WEBHOOK_URL へ Incoming Webhook で POST

将来 Slack Bot (chat.postMessage / Socket Mode) に差し替える場合も、
Notifier インターフェースの背後で置き換える。
"""
import json
import os
import urllib.request
from abc import ABC, abstractmethod

# 隅付き括弧 (メッセージの見出し用)。ソースには chr() で書く
LB = chr(0x3010)
RB = chr(0x3011)


def build_review1_message(title, editor_url, summary):
    """確認依頼1: 自動編集の完了通知。エディタへのリンク付き。"""
    return (
        f"{LB}確認依頼1{RB}動画編集の確認をお願いします\n"
        f"タイトル: {title}\n"
        f"{summary}\n"
        f"エディタ: {editor_url}\n"
        "問題なければエディタで「編集する」を押してください"
        "（開始点やカットの微修正もできます）。"
        "公開しない回はここで止めてください。"
    )


def build_review2_message(title, youtube_url, summary):
    """確認依頼2: YouTube非公開アップロードの完了通知。最終確認用。"""
    return (
        f"{LB}確認依頼2{RB}YouTubeに非公開でアップロードしました。"
        "最終確認をお願いします\n"
        f"タイトル: {title}\n"
        f"{summary}\n"
        f"YouTube: {youtube_url}\n"
        "問題なければ公開に切り替えてください。"
    )


def build_error_message(title, detail):
    """エラー通知: 推測で進めず止まっていることを伝える。"""
    return (
        f"{LB}エラー{RB}動画編集パイプラインで問題が発生しました\n"
        f"対象: {title}\n"
        f"内容: {detail}\n"
        "推測で進めず止まっています。確認をお願いします。"
    )


class Notifier(ABC):
    """通知のインターフェース。"""

    @abstractmethod
    def send(self, text):
        """メッセージを1件送る。"""

    def notify_review1(self, title, editor_url, summary):
        self.send(build_review1_message(title, editor_url, summary))

    def notify_review2(self, title, youtube_url, summary):
        self.send(build_review2_message(title, youtube_url, summary))

    def notify_error(self, title, detail):
        self.send(build_error_message(title, detail))


class ConsoleNotifier(Notifier):
    """標準出力に表示する通知 (デモ・開発用)。"""

    def send(self, text):
        print("\n----- Slack通知 (console) -----")
        print(text)
        print("-------------------------------\n", flush=True)


class WebhookNotifier(Notifier):
    """Slack Incoming Webhook へ stdlib の urllib で POST する。"""

    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
        if not self.webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL が設定されていません")

    def send(self, text):
        req = urllib.request.Request(
            self.webhook_url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as res:
            if res.status != 200:
                raise RuntimeError(f"Slack Webhook が {res.status} を返しました")


def get_notifier():
    """SLACK_WEBHOOK_URL があれば Webhook、無ければ Console を返す。"""
    if os.environ.get("SLACK_WEBHOOK_URL"):
        return WebhookNotifier()
    return ConsoleNotifier()
