#!/usr/bin/env bash
# Mac から Windows へ渡す用 ZIP（日本語フォルダ名・.venv を除く）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
NAME="MiteneAutoSend"
ZIP="$OUT/${NAME}-windows.zip"

mkdir -p "$OUT"
rm -f "$ZIP"

cd "$ROOT"
zip -r "$ZIP" . \
  -x "*.git*" \
  -x "*/.venv/*" \
  -x "*/.venv" \
  -x "*/build/*" \
  -x "*/dist/*" \
  -x "*/logs/*" \
  -x "*/screenshots/*" \
  -x "*/playwright-browsers/*" \
  -x "*/.DS_Store" \
  -x "*/*.app/*" \
  -x "*/__pycache__/*" \
  -x "*/data/accounts.json" \
  -x "*/data/settings.json"

echo ""
echo "作成しました: $ZIP"
echo "Windows で展開後 → install-browser.bat → open-browser.bat"
