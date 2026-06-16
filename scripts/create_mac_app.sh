#!/bin/bash
# macOS用 .app をプロジェクト内に作成
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

APP_NAME="ミテネ自動送信.app"
MACOS_DIR="$APP_NAME/Contents/MacOS"
RES_DIR="$APP_NAME/Contents/Resources"

rm -rf "$APP_NAME"
mkdir -p "$MACOS_DIR" "$RES_DIR"

cat > "$APP_NAME/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>ja_JP</string>
  <key>CFBundleExecutable</key>
  <string>launch</string>
  <key>CFBundleIconFile</key>
  <string>icon</string>
  <key>CFBundleIdentifier</key>
  <string>local.mitene.sender</string>
  <key>CFBundleName</key>
  <string>ミテネ自動送信</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

if [ ! -f web/static/icon.icns ]; then
  echo "アイコンを生成しています…"
  "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/build_app_icon.py" 2>/dev/null \
    || python3 "$PROJECT_ROOT/scripts/build_app_icon.py"
fi
cp web/static/icon.icns "$RES_DIR/icon.icns"

cat > "$MACOS_DIR/launch" << 'LAUNCH'
#!/bin/bash
PROJECT_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
  osascript -e 'display notification "初回セットアップを開始します" with title "ミテネ自動送信"'
fi

exec "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/launch_app.py" 2>>"$PROJECT_ROOT/logs/app.log"
LAUNCH

chmod +x "$MACOS_DIR/launch"

echo "作成しました: $PROJECT_ROOT/$APP_NAME"
echo "Finder でダブルクリックして起動できます。"
