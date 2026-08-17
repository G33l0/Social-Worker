@echo off
REM Build a distributable Social Worker package (wheel + sdist)
setlocal
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat

echo Running checks before building...
python -m compileall -q app cli mock_site main.py || exit /b 1
python -m pytest -q || exit /b 1

echo Building the package...
python -m pip install --upgrade build || exit /b 1
python -m build || exit /b 1

echo [OK] Artifacts written to dist\
endlocal
