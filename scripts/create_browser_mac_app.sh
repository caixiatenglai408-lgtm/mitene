#!/bin/bash
# 「ブラウザで開く.command」と同じ動作の .app（デスクトップ用アイコン付き）
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

APP_NAME="ブラウザで開く.app"
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
  <string>local.mitene.sender.browser</string>
  <key>CFBundleName</key>
  <string>ブラウザで開く</string>
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
CMD="$PROJECT_ROOT/ブラウザで開く.command"
if [ ! -f "$CMD" ]; then
  osascript -e 'display alert "ブラウザで開く.command が見つかりません。プロジェクトフォルダを移動していないか確認してください。" as critical'
  exit 1
fi
xattr -d com.apple.quarantine "$CMD" 2>/dev/null || true
open -a Terminal "$CMD"
LAUNCH

chmod +x "$MACOS_DIR/launch"

echo "作成しました: $PROJECT_ROOT/$APP_NAME"
