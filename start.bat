@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

:: 先背景補抓缺少的資料
start /b "" "%PY%" auto_update.py

:: 啟動網站 + 開瀏覽器
start "" http://127.0.0.1:5000
"%PY%" app.py
