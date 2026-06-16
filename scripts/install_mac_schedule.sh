#!/bin/bash
# macOS 定時送信（スリープ対策）を手動で再登録
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

if [ "$(uname)" != "Darwin" ]; then
  echo "macOS のみ対応です"
  exit 1
fi

PYTHON="${ROOT}/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "先に 起動.command で venv を作成してください"
  exit 1
fi

"$PYTHON" -c "
import json, sys
sys.path.insert(0, 'src')
from platform_schedule import sync_platform_schedule
print(json.dumps(sync_platform_schedule(), ensure_ascii=False, indent=2))
"
