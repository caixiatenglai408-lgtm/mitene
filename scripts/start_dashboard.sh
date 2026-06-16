#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  echo "エラー: .venv がありません。README のセットアップを実行してください。"
  exit 1
fi

source .venv/bin/activate
export PYTHONPATH="${PWD}/src:${PYTHONPATH:-}"

echo ""
echo "=========================================="
echo "  管理画面: http://localhost:5050"
echo "  終了: Ctrl+C（このターミナルを閉じないでください）"
echo "=========================================="
echo ""

python web/app.py
