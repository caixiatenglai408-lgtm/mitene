#!/bin/bash
# cron 例: 0 10 * * * /path/to/自動送信機能/scripts/run_daily.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
python src/main.py >> logs/cron.log 2>&1
