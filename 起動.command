#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "初回セットアップ中..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  playwright install chromium
else
  source .venv/bin/activate
fi

if ! python scripts/ensure_playwright_browsers.py; then
  echo ""
  echo "ブラウザのダウンロードに失敗しました。"
  echo "「ブラウザをインストール.command」を実行してから再度お試しください。"
  read -r -p "Enterキーで閉じます… " _
  exit 1
fi

mkdir -p logs

if [ ! -f web/static/icon.icns ]; then
  python scripts/build_app_icon.py 2>/dev/null || true
fi

echo ""
echo "=========================================="
echo "  アプリウィンドウで起動します"
echo "  URL: http://127.0.0.1:5050"
echo "  ブラウザで開く場合: python launch_app.py --browser"
echo "=========================================="
echo ""

python launch_app.py
