@echo off
setlocal

set "LOG=D:\claude\tw-stock-scanner\autostart.log"
set "PYW=C:\Users\User\AppData\Local\Programs\Python\Python312\pythonw.exe"
set "PRJ=D:\claude\tw-stock-scanner"

echo [%date% %time%] === autostart triggered ===>>"%LOG%"

netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [%date% %time%] port 5000 already listening, skip>>"%LOG%"
    exit /b 0
)

if not exist "%PYW%" (
    echo [%date% %time%] ERROR pythonw not found at %PYW%>>"%LOG%"
    exit /b 1
)

cd /d "%PRJ%"
if errorlevel 1 (
    echo [%date% %time%] ERROR cannot cd to %PRJ%>>"%LOG%"
    exit /b 1
)

start "" "%PYW%" watchdog.py
echo [%date% %time%] watchdog.py launched>>"%LOG%"

start "" "%PYW%" auto_update.py
echo [%date% %time%] auto_update.py launched>>"%LOG%"

exit /b 0
