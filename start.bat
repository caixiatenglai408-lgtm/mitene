@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PYTHON=%~dp0.venv\Scripts\python.exe
set PIP=%~dp0.venv\Scripts\pip.exe

if not exist "%PYTHON%" (
  echo First-time setup...
  python -m venv .venv
  "%PIP%" install -r requirements.txt
  "%PYTHON%" -m playwright install chromium
)

if not exist "%PYTHON%" (
  echo Python not found. Install from https://www.python.org/downloads/
  pause
  exit /b 1
)

"%PYTHON%" scripts\ensure_playwright_browsers.py
if errorlevel 1 (
  echo.
  echo Browser install failed. Run install-browser.bat first.
  pause
  exit /b 1
)

if not exist logs mkdir logs

echo.
echo ==========================================
echo   Starting app window
echo   URL: http://127.0.0.1:5050
echo   Browser mode: open-browser.bat
echo ==========================================
echo.

"%PYTHON%" launch_app.py
pause
