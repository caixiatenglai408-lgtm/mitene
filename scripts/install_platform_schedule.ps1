# Windows: OS 定時登録を手動で再同期
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$python = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "先に venv を作成してください: python -m venv .venv"
    exit 1
}

$env:PYTHONPATH = Join-Path $PWD "src"
& $python -c @"
import json, sys
sys.path.insert(0, 'src')
from platform_schedule import sync_platform_schedule
print(json.dumps(sync_platform_schedule(), ensure_ascii=False, indent=2))
"@
