#!/bin/bash
# ダブルクリックでブラウザに管理画面を開く
set -e
cd "$(dirname "$0")"

xattr -d com.apple.quarantine "$0" 2>/dev/null || true

ROOT="$(pwd)"
PYTHON="$ROOT/.venv/bin/python"
PIP="$ROOT/.venv/bin/pip"

pause_on_error() {
  echo ""
  echo "起動に失敗しました。上のメッセージを確認してください。"
  echo "（すでに起動中の場合は 起動.command を一度終了してから再度お試しください）"
  read -r -p "Enterキーで閉じます… " _
}

trap pause_on_error ERR

if [ ! -x "$PYTHON" ]; then
  echo "初回セットアップ中（数分かかります）..."
  python3 -m venv .venv
  "$PIP" install -r requirements.txt
  "$PYTHON" -m playwright install chromium
fi

if ! "$PYTHON" scripts/ensure_playwright_browsers.py; then
  echo ""
  echo "ブラウザのダウンロードに失敗しました。"
  echo "「ブラウザをインストール.command」をダブルクリックしてから再度お試しください。"
  pause_on_error
  exit 1
fi

mkdir -p logs

printf '\033]0;ミテネ管理画面\007'

echo ""
echo "=========================================="
echo "  ブラウザで管理画面を開きます"
echo "  URL: http://127.0.0.1:5050"
echo "  終了: Chrome のタブを閉じるとこの画面も閉じます"
echo "=========================================="
echo ""

"$PYTHON" launch_app.py --browser --chrome || { pause_on_error; exit 1; }

( sleep 0.4; "$(dirname "$0")/scripts/close_mitene_terminal.sh" ) >/dev/null 2>&1 &
exit 0
