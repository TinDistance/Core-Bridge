@echo off
cd /d "%~dp0"
echo Cleaning Old Server Processes...
powershell -NoProfile -NonInteractive -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*server.app:app*' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>$null }" >nul 2>&1
timeout /t 2 /nobreak >nul
echo Cleaning Python Cache...
for %%D in (desktop server test k230) do (
    if exist "%%D" for /d /r "%%D" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
)
del /s /q *.pyc >nul 2>&1
echo Cleaning server logs...
if exist "logs\server.out" del /q "logs\server.out" >nul 2>&1
echo Starting desktop app...
python -m desktop.app
if errorlevel 1 (
    echo.
    echo Failed to start desktop app. Check that Python and dependencies are installed:
    echo     pip install -r requirements.txt
    pause
)
