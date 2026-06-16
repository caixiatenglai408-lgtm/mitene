#!/bin/bash
# Mac / Windows 共通: OS 定時登録を手動で再同期（Mac では bash、Win では .ps1 を使用）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(uname)" = "Darwin" ]; then
  PYTHON="${PWD}/.venv/bin/python"
elif [ "$(uname)" = "MINGW"* ] || [ "$(uname)" = "MSYS"* ] || [ -n "${WINDIR:-}" ]; then
  exec powershell -NoProfile -ExecutionPolicy Bypass -File "$(dirname "$0")/install_platform_schedule.ps1"
else
  echo "Mac または Windows で実行してください"
  exit 1
fi

if [ ! -x "$PYTHON" ]; then
  echo "先に venv を作成してください（起動.command または python -m venv .venv）"
  exit 1
fi

"$PYTHON" -c "
import json, sys
sys.path.insert(0, 'src')
from platform_schedule import sync_platform_schedule
print(json.dumps(sync_platform_schedule(), ensure_ascii=False, indent=2))
"
