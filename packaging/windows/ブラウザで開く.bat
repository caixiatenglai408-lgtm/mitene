@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "MiteneAutoSend.exe" (
  echo MiteneAutoSend.exe が見つかりません。
  pause
  exit /b 1
)

if not exist logs mkdir logs

echo.
echo ==========================================
echo   ブラウザで管理画面を開きます
echo   URL: http://127.0.0.1:5050
echo   終了: ブラウザのタブをすべて閉じる
echo ==========================================
echo.

MiteneAutoSend.exe --browser
pause
