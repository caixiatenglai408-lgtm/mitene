@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PYTHON=%~dp0.venv\Scripts\python.exe
set PIP=%~dp0.venv\Scripts\pip.exe

if not exist "%PYTHON%" (
  python -m venv .venv
  "%PIP%" install -r requirements.txt
)

"%PYTHON%" -m playwright install chromium
"%PYTHON%" scripts\ensure_playwright_browsers.py

echo.
echo 完了しました。次に「ブラウザで開く.bat」で起動してください。
pause
