@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
 echo Install Python and create .venv first.
 pause
 exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -u app.py
