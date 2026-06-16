#!/bin/bash
# この .command を実行している Terminal ウィンドウだけを閉じる（macOS）
[ "$(uname -s)" = "Darwin" ] || exit 0

TTY_DEV="$(tty 2>/dev/null | sed 's|^/dev/||')"
if [ -z "$TTY_DEV" ] || ! [[ "$TTY_DEV" =~ ^[a-zA-Z0-9]+$ ]]; then
  exit 0
fi

osascript <<APPLESCRIPT 2>/dev/null || true
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set tabTty to tty of t
        if tabTty contains "${TTY_DEV}" then
          close w saving no
          return
        end if
      end try
    end repeat
  end repeat
  repeat with w in windows
    set winName to name of w as text
    if winName contains "ミテネ管理画面" or winName contains "ブラウザで開く" then
      close w saving no
      return
    end if
  end repeat
end tell
APPLESCRIPT
