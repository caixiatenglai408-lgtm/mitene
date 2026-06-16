#!/bin/bash
# 送信の数分前に Mac をスリープから起こす予約（要パスワード・任意）
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PYTHON="${ROOT}/.venv/bin/python"

if [ "$(uname)" != "Darwin" ]; then
  echo "macOS のみ対応です"
  exit 1
fi
if [ ! -x "$PYTHON" ]; then
  echo "先に 起動.command で venv を作成してください"
  exit 1
fi

echo "送信時刻の数分前に Mac を起こす予約を登録します（管理者パスワードが必要です）"
echo ""

WAKE_LINES=$("$PYTHON" -c "
import sys
from pathlib import Path
ROOT = Path('$ROOT')
sys.path.insert(0, str(ROOT / 'src'))
from platform_schedule.common import iter_upcoming_wake_times
from store import load_settings
wakes = iter_upcoming_wake_times(load_settings())
if not wakes:
    sys.exit(2)
for w in wakes:
    print(w.strftime('%m/%d/%y %H:%M:%S'))
" ) || {
  echo "有効な送信スケジュールがありません（自動送信ON・送信時間ONを確認）"
  exit 1
}

count=0
while IFS= read -r stamp; do
  [ -z "$stamp" ] && continue
  echo "  wake $stamp"
  if sudo pmset schedule wakeorpoweron "$stamp"; then
    count=$((count + 1))
  fi
done <<< "$WAKE_LINES"

echo ""
echo "登録しました: ${count} 件"
echo "確認: pmset -g sched"
