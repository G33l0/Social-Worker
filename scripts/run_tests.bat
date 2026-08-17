@echo off
REM Run the Social Worker test suite
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python -m pytest -q %*
