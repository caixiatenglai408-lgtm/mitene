# Windows 用 .exe を PyInstaller でビルドする
# 使い方: PowerShell でこのスクリプトを実行
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\build_windows_exe.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== ミテネ自動送信 Windows ビルド ===" -ForegroundColor Cyan

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "仮想環境を作成しています..."
    python -m venv .venv
    & $Python -m pip install -r requirements.txt -r requirements-build.txt
}

& $Python -m pip install -r requirements.txt -r requirements-build.txt
& $Python -m PyInstaller --noconfirm --clean mitene_autosend.spec

$Dist = Join-Path $Root "dist\MiteneAutoSend"
if (-not (Test-Path (Join-Path $Dist "MiteneAutoSend.exe"))) {
    Write-Error "ビルド失敗: MiteneAutoSend.exe が見つかりません"
}

$Packaging = Join-Path $Root "packaging\windows"
foreach ($name in @("起動.bat", "ブラウザで開く.bat", "ブラウザをインストール.bat", "使い方.txt")) {
    $src = Join-Path $Packaging $name
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Dist $name) -Force
    }
}

Write-Host ""
Write-Host "完了: $Dist" -ForegroundColor Green
Write-Host "配布フォルダ dist\MiteneAutoSend を ZIP にして配布してください。"
