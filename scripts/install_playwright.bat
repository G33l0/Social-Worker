@echo off
REM Install the Playwright browser binaries used by Social Worker
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python -m playwright install chromium
python -m playwright install-deps chromium 2>nul
echo [OK] Chromium ready.
