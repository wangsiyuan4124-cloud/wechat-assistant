@echo off
rem WeChat group sender service launcher (called by Task Scheduler)
cd /d "%~dp0"

rem Guard: exit if an app.py pythonw instance is already running
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter 'Name=''pythonw.exe''' | Where-Object { $_.CommandLine -match 'app\.py' }) { exit 1 }"
if errorlevel 1 (
    echo [run-sender] service already running, skip.
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)

start "" .venv\Scripts\pythonw.exe -u app.py
