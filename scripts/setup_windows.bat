@echo off
REM Social Worker - first-time setup for Windows
setlocal

echo ==========================================================
echo  SOCIAL WORKER - SETUP
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python was not found on PATH. Install Python 3.11+ first.
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || exit /b 1
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements.txt || exit /b 1

echo Installing the Chromium browser for Playwright...
python -m playwright install chromium || exit /b 1

echo Initialising the database...
python main.py --init-db || exit /b 1

echo.
echo [OK] Setup complete. Start the console with:
echo      .venv\Scripts\activate.bat
echo      python main.py
endlocal
