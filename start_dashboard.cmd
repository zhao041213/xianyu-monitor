@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found.
  echo Run the installation commands in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "dashboard.py"
if errorlevel 1 pause
