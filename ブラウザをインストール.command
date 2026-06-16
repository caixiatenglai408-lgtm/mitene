#!/bin/bash
# 送信に必要な Chromium をダウンロード（ダブルクリックで実行）
set -e
cd "$(dirname "$0")"

xattr -d com.apple.quarantine "$0" 2>/dev/null || true

ROOT="$(pwd)"
PYTHON="$ROOT/.venv/bin/python"
PIP="$ROOT/.venv/bin/pip"

echo ""
echo "=========================================="
echo "  自動送信用ブラウザのセットアップ"
echo "=========================================="
echo ""

if [ ! -x "$PYTHON" ]; then
  echo "Python 環境を作成しています（初回のみ）..."
  python3 -m venv .venv
  "$PIP" install -r requirements.txt
fi

"$PYTHON" scripts/ensure_playwright_browsers.py
code=$?

echo ""
if [ "$code" -eq 0 ]; then
  echo "完了しました。次に「ブラウザで開く.command」で起動し、"
  echo "「今すぐ全員送信」をお試しください。"
else
  echo "ダウンロードに失敗しました。Wi-Fi を確認して再度実行してください。"
fi
echo ""
read -r -p "Enterキーで閉じます… " _
