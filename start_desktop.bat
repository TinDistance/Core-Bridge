@echo off
cd /d "%~dp0"
rem Kill stale uvicorn on port 8000 so the fresh code (e.g. raw-SDP /webrtc/push) is served.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
python -m desktop.app
if errorlevel 1 (
    echo.
    echo Failed to start desktop app. Check that Python and dependencies are installed:
    echo     pip install -r requirements.txt
    pause
)
