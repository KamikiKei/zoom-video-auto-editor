# 動画編集パイプライン (pipeline/)

Zoom録画の公開までの定常作業をなくすためのパイプライン骨格。
設計は `docs/基本設計.md`、要件は `docs/要件定義.md` を参照。

現時点では外部アカウント・認証情報が無いため、外部接続はモック/スタブで
置き換えてあり、**Zoom取得以外の全工程がローカルで通しで動く**。

## フロー

```
Zoom録画完了
  -> 取り込み (zoom_client)
  -> エディタジョブ作成: 動画・VTT・素材・カット案 plan を事前投入 (editor_bridge)
  -> Slack確認依頼1: エディタへのリンクを通知 (notify)
  -> 人間がエディタで微修正 (任意): 登壇者の複数選択 / カット候補の残す・切る
  -> エクスポート (エディタの「編集する」= output.mp4 生成)
  -> 概要欄生成 (description)
  -> YouTube非公開アップロード (youtube_client)
  -> Slack確認依頼2: YouTubeリンクを通知
  -> 人間が最終確認して公開
```

デーモンは「確認待ち」ジョブを次回実行時に拾って後段を進めるので、
人間の書き出しを待つ間にプロセスが居座ることはない。

## 動かし方

```bash
# 通しデモ (合成録画で全工程。人間の書き出しは自動で再現)
bash pipeline/run_demo.sh

# 通常実行 (モックinboxに録画を置いてから)
python3 -m pipeline.daemon --once
python3 -m pipeline.daemon --once --demo   # 全工程を1回で通す
python3 -m pipeline.daemon --poll --interval 300
```

モックの inbox 構成 (`pipeline/inbox/<録画ID>/`):

| ファイル | 内容 |
|---|---|
| `video.mp4` | 録画本体 (必須) |
| `transcript.vtt` | Zoom文字起こし (任意) |
| `thumb.png` 等 | OP/ED素材 (任意。`thumb.*` を探す) |
| `meta.json` | タイトル・登壇者・回数などのメタ情報 (任意) |

## モジュール構成

| モジュール | 役割 |
|---|---|
| `daemon.py` | 全体の指揮。ポーリング/ワンショット実行、状態遷移の管理 |
| `zoom_client.py` | 録画一覧・ダウンロードのインターフェース。`MockZoomClient` 実装済み、`ZoomS2SClient` はスタブ |
| `editor_bridge.py` | app.py の `jobs/` にジョブを作成し plan 等を事前投入。書き出し再現 (render_job) と要約生成もここ |
| `notify.py` | Slack通知。`ConsoleNotifier` と `WebhookNotifier` (SLACK_WEBHOOK_URL)。確認依頼1/2とエラー通知の文面生成 |
| `youtube_client.py` | アップロードのインターフェース。`MockYouTubeClient` 実装済み (outboxへコピー+ダミーURL)、実接続はスタブ |
| `description.py` | タイトル・概要欄の生成 (固定部品 + meta の connpass 相当ブロック) |
| `state.py` | 状態の JSON 永続化 (処理済み録画の再処理防止) |
| `make_demo_assets.py` / `run_demo.sh` | デモ用合成素材の生成と通しデモ |

## エディタ連携の仕様

- ジョブは app.py と同じ `jobs/<job_id>/` 構造 (input.mp4 / transcript.vtt /
  thumb_asset / plan.json / diarization.json / samples/ / jobmeta.json)
- エディタURL: `{EDITOR_BASE_URL}/?job=<job_id>` (既定 `http://localhost:8765/?job=...`)
- app.py は `?job=` で開かれると jobinfo API から plan・話者解析を読み込み、
  動画プレビュー・タイムライン・開始/終了・カット候補を事前投入した状態で開く
- **登壇者は複数選択可**: 話者カードのチェックボックスで選ぶ。
  plan の `presenters` に話者インデックスのリストが入り、開始点は
  「選んだ登壇者のうち最初に5秒以上話し始めた人」の位置になる
- **カット候補は区間ごとに残す/切るを選べる**: plan の
  `silence.candidates` (enabled フラグ付き) を一覧表示し、外した区間は
  `disabled_cuts` として process に渡され、保持区間 (keeps) が再計算される。
  render は plan の keeps で実行される
- 手動ジョブでも同じ操作ができる (話者解析後に `/api/silences` で
  カット候補を取得する)

## 環境変数

| 変数 | 用途 | 既定 |
|---|---|---|
| `PIPELINE_INBOX` | モック用 inbox | `pipeline/inbox` |
| `PIPELINE_OUTBOX` | モックYouTubeの出力先 | `pipeline/outbox` |
| `PIPELINE_STATE` | 状態JSON | `pipeline/state.json` |
| `PIPELINE_WORK` | ダウンロード作業領域 | `pipeline/work` |
| `EDITOR_BASE_URL` | エディタのURL | `http://localhost:8765` |
| `SLACK_WEBHOOK_URL` | あれば Webhook 通知、無ければコンソール | - |
| `ZOOM_ACCOUNT_ID` / `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` | Zoom S2S 認証情報 (実接続用) | - |
| `YOUTUBE_CLIENT_SECRETS` / `YOUTUBE_TOKEN_FILE` | YouTube OAuth 用 (実接続用) | - |

## 実サービスへの差し替え手順

1. **Zoom**: S2S OAuth アプリを所有者/管理者アカウントで作成し
   (メンバー権限では作成不可。docs/基本設計.md 参照)、上記3つの環境変数を
   設定する。`ZoomS2SClient` の docstring に利用エンドポイント
   (トークン取得・録画一覧・ファイル取得) を明記してあるので、それに沿って
   実装する。`ZOOM_CLIENT_ID` が設定されるとデーモンは実接続クラスを選ぶ。
2. **Slack**: Incoming Webhook の URL を `SLACK_WEBHOOK_URL` に設定するだけで
   WebhookNotifier に切り替わる。Bot トークン方式にする場合は
   Notifier インターフェースの背後で実装を差し替える。
3. **YouTube**: Google Cloud で YouTube Data API v3 を有効化し OAuth 認可する。
   手順と TODO は `YouTubeDataAPIClient` の docstring に明記。
   `YOUTUBE_CLIENT_SECRETS` が設定されるとデーモンは実接続クラスを選ぶ。
4. **OP/ED・サムネ素材**: 実運用では Zoom 録画に含まれないため、Canva API /
   Google Drive エクスポート等から取得する工程を daemon の前段に追加する
   (現モックでは inbox に同梱された `thumb.*` を使う)。

## 現時点の制限・TODO

- Zoom / YouTube の実接続はスタブ (NotImplementedError)。認証情報の入手待ち
- 素材 (OP/ED・サムネ) の自動取得は未実装 (モックでは inbox 同梱)
- 概要欄の定型文は仮置き。実際のチャンネルの文面と最終調整が必要
- イベント回 (特別回) のタイトルは自動決定せず人間確認に回す実装
  (title_override が無い場合はエラー通知して止まる)
- エディタのアクセス制御は未実装 (信頼できるネットワーク内での利用が前提)
- カット候補は現在「無音」のみ。発言単位の候補 (つなぎの言葉など) は未対応
- 画面共有区間の自動検出は未対応。画面共有中の音声は発表の一部であり
  消してはいけないため、現状はエディタのタイムラインでカット候補を
  ワンクリックで「残す」に戻す運用でカバーする (検出は将来課題)
- 複数登壇者の開始点ルールは「最初に5秒以上話す人」。短い相槌を
  どう扱うかは実素材での調整余地あり
