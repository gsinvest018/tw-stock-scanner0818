@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
taskkill /F /IM ngrok.exe 2>nul
"%PY%" watchdog.py
