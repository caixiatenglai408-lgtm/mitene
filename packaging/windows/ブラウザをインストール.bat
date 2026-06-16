@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "MiteneAutoSend.exe" (
  echo MiteneAutoSend.exe が見つかりません。
  pause
  exit /b 1
)

echo.
echo Chromium をダウンロードします（初回のみ・数分かかります）
echo.

MiteneAutoSend.exe --install-browsers
pause
