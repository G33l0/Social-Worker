@echo off
REM Start the bundled mock forum website on http://127.0.0.1:8099
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python mock_site\test_site.py --port 8099 %*
