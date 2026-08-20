@echo off
echo ========================================
echo  台股掃描器 - 歷史資料回補
echo  範圍: 2025-04-01 ~ 2026-03-09
echo  預估時間: 約 60~90 分鐘
echo ========================================
echo.
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" run_daily.py 20250401 20260309
echo.
echo ========================================
echo  回補完成！可以啟動網站了：
echo  python app.py
echo ========================================
pause
