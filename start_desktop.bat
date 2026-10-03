@echo off
cd /d "%~dp0"
rem Desktop 会自己拉起 server 子进程，无需在此杀端口。
rem 如需手动清理 8000 端口，请确认 PID 后再 taskkill（避免误杀 :18000 等）。
if not exist ".venv\Scripts\python.exe" (
    echo [hint] 未检测到 .venv，建议先创建虚拟环境并安装依赖：
    echo     python -m venv .venv ^&^& .venv\Scripts\python -m pip install -r requirements.txt
)
python -m desktop.app
if errorlevel 1 (
    echo.
    echo Failed to start desktop app. Check that Python and dependencies are installed:
    echo     pip install -r requirements.txt
    pause
)
