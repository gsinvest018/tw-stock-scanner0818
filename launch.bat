@echo off
cd /d D:\claude\tw-stock-scanner
echo === 台股掃描器啟動中 ===

:: Kill old processes
taskkill /F /IM ngrok.exe 2>nul
for /f "tokens=2" %%i in ('tasklist ^| findstr /i "python"') do taskkill /F /PID %%i 2>nul
timeout /t 2 /nobreak >nul

:: Start watchdog via PowerShell (truly detached, survives terminal close)
powershell -Command "Start-Process -FilePath 'C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe' -ArgumentList 'watchdog.py' -WorkingDirectory 'D:\claude\tw-stock-scanner' -WindowStyle Minimized"

echo Watchdog launched, waiting for services...
timeout /t 15 /nobreak >nul
start "" http://127.0.0.1:5000
echo === Done ===
