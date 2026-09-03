@echo off
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%\"

set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PIP=%ROOT%.venv\Scripts\pip.exe"

where python >nul 2>&1
if errorlevel 1 (
  echo Python not found.
  echo Install from https://www.python.org/downloads/
  echo Check "Add python.exe to PATH" during install.
  exit /b 1
)

if not exist "%PYTHON%" (
  echo Creating virtual environment...
  python -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo Failed to create .venv
    exit /b 1
  )
)

"%PYTHON%" -c "import sys; print(sys.version)" >nul 2>&1
if errorlevel 1 (
  echo .venv is broken ^(maybe copied from Mac^).
  echo Delete the .venv folder and run this again.
  exit /b 1
)

"%PYTHON%" -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo Installing Python packages...
  "%PYTHON%" -m pip install --upgrade pip
  "%PIP%" install -r "%ROOT%requirements.txt"
  if errorlevel 1 (
    echo pip install failed.
    exit /b 1
  )
)

"%PYTHON%" -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo Playwright install failed.
  echo Try: delete .venv folder, then run install-browser.bat again.
  exit /b 1
)

exit /b 0
