@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Run setup.bat first.
  pause
  exit /b 1
)

echo Installing the Windows packaging tool...
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed

echo Building a diagnostic folder version...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name LogiBattery-debug --icon assets\LogiBattery.ico --distpath dist\debug --workpath build\debug battery_monitor.py
if errorlevel 1 goto failed

echo Building the standalone single-file app...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name LogiBattery --icon assets\LogiBattery.ico --distpath dist --workpath build\onefile battery_monitor.py
if errorlevel 1 goto failed

echo.
echo Build complete: dist\LogiBattery.exe
pause
exit /b 0

:failed
echo.
echo Build failed. Send the error output above for troubleshooting.
pause
exit /b 1
