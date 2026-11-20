#!/bin/bash
# Zoom動画 かんたん編集（ローカル版） Mac / Linux 用ランチャー
cd "$(dirname "$0")"
echo "========================================"
echo " Zoom動画 かんたん編集（ローカル版）"
echo "========================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[不足] Python3 が見つかりません。https://www.python.org から入れてください。"
  read -r -p "Enterで閉じます" _
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[不足] ffmpeg が見つかりません。"
  echo "  Mac:  brew install ffmpeg"
  echo "  Linux: sudo apt install ffmpeg"
  read -r -p "Enterで閉じます" _
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "初回セットアップ中です。少し時間がかかります..."
  python3 -m venv .venv 2>/dev/null || true
fi

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi

if ! "$PY" -c "import numpy, scipy" 2>/dev/null; then
  echo "必要な部品をインストールしています..."
  "$PY" -m pip install numpy scipy || "$PY" -m pip install --user numpy scipy
fi

echo ""
echo "起動します。ブラウザで http://localhost:8765 を開いてください。"
echo "終わったら、この画面で Ctrl+C を押してください。"
echo ""
( sleep 2; (command -v open >/dev/null && open http://localhost:8765) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8765) || true ) &
exec "$PY" app.py
