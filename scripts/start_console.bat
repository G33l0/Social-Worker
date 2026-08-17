@echo off
REM Launch the Social Worker management console
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python main.py %*
