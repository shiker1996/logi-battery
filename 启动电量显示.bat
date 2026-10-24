@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Run setup.bat first.
  pause
  exit /b 1
)
echo Starting LogiBattery. The tray icon appears near the clock.
echo Keep this window open. Startup errors will be shown here.
".venv\Scripts\python.exe" "battery_monitor.py"
if errorlevel 1 (
  echo.
  echo LogiBattery stopped with an error. Send the message above for troubleshooting.
  pause
)
