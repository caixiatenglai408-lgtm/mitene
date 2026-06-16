#!/bin/bash
# デスクトップに「ブラウザで開く」のアプリ風ショートカット（エイリアス）を置く
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
DESKTOP="$HOME/Desktop"
SHORTCUT_NAME="ミテネ管理画面"
APP_PATH="$PROJECT_ROOT/ブラウザで開く.app"
ALIAS_PATH="$DESKTOP/${SHORTCUT_NAME}"

chmod +x scripts/create_browser_mac_app.sh
./scripts/create_browser_mac_app.sh

# 既存のエイリアス／同名ファイルを削除
rm -f "${ALIAS_PATH}" "${ALIAS_PATH}.app" "${ALIAS_PATH}.alias" 2>/dev/null || true

osascript <<EOF
tell application "Finder"
  set desktopFolder to POSIX file "$DESKTOP" as alias
  set sourceApp to POSIX file "$APP_PATH" as alias
  make new alias file at desktopFolder to sourceApp with properties {name:"$SHORTCUT_NAME"}
end tell
EOF

echo ""
echo "デスクトップにショートカットを作成しました:"
echo "  $DESKTOP/$SHORTCUT_NAME"
echo ""
echo "アイコンをダブルクリックすると、これまでと同じくブラウザで管理画面が開きます。"
echo "（1枚目の Chrome などと同じ「矢印付き」ショートカットです）"
