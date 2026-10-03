@echo off
cd /d "%~dp0"
python -m desktop.app
if errorlevel 1 (
    echo.
    echo Failed to start desktop app. Check that Python and dependencies are installed:
    echo     pip install -r requirements.txt
    pause
)
