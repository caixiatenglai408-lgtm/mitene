@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PYTHON=%~dp0.venv\Scripts\python.exe
set PIP=%~dp0.venv\Scripts\pip.exe

if not exist "%PYTHON%" (
  echo 初回セットアップ中...
  python -m venv .venv
  "%PIP%" install -r requirements.txt
  "%PYTHON%" -m playwright install chromium
)

if not exist "%PYTHON%" (
  echo Python が見つかりません。https://www.python.org/downloads/ からインストールしてください。
  pause
  exit /b 1
)

"%PYTHON%" scripts\ensure_playwright_browsers.py
if errorlevel 1 (
  echo.
  echo ブラウザのダウンロードに失敗しました。
  echo 「ブラウザをインストール.bat」を実行してから再度お試しください。
  pause
  exit /b 1
)

if not exist logs mkdir logs

echo.
echo ==========================================
echo   アプリウィンドウで起動します
echo   URL: http://127.0.0.1:5050
echo   ブラウザで開く場合: ブラウザで開く.bat
echo ==========================================
echo.

"%PYTHON%" launch_app.py
pause
