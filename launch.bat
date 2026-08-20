@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo === 台股掃描器啟動中 ===

:: Kill old processes
taskkill /F /IM ngrok.exe 2>nul
for /f "tokens=2" %%i in ('tasklist ^| findstr /i "python"') do taskkill /F /PID %%i 2>nul
timeout /t 2 /nobreak >nul

:: Start watchdog via PowerShell (truly detached, survives terminal close)
powershell -Command "Start-Process -FilePath '%PY%' -ArgumentList 'watchdog.py' -WorkingDirectory '%~dp0' -WindowStyle Minimized"

echo Watchdog launched, waiting for services...
timeout /t 15 /nobreak >nul
start "" http://127.0.0.1:5000
echo === Done ===
