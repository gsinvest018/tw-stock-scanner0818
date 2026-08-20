@echo off
setlocal

set "LOG=%~dp0autostart.log"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PRJ=%~dp0"

echo [%date% %time%] === autostart triggered ===>>"%LOG%"

netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [%date% %time%] port 5000 already listening, skip>>"%LOG%"
    exit /b 0
)

if not exist "%PYW%" set "PYW=pythonw"

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
