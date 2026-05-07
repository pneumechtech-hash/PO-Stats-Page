@echo off
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Installing dependencies...
venv\Scripts\pip install -q flask

echo.
echo Starting PO Tracker Stats at http://localhost:5000
echo Press Ctrl+C to stop.
echo.
venv\Scripts\python app.py
pause
