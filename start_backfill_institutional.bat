@echo off
REM ============================================================
REM 三大法人買賣超 - 全歷史回補 + 打包
REM   1. 從 API 最早日期回補到今天 (TWSE 2012-05-02 / TPEx 2018-01-15)
REM   2. 自動跳過 DB 已有日期
REM   3. 結束後產出 data/institutional_full.parquet
REM
REM 預估時間: 約 6 小時 (取決於網路與 API 反應)
REM   - 跑到一半關掉沒關係，下次接續跑
REM   - 視窗請保持開啟，不要登出 Windows
REM ============================================================
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo ====== Step 1/2: 回補 institutional 資料 ======
echo.
"%PY%" backfill_institutional.py
if errorlevel 1 (
    echo.
    echo [!] 回補階段失敗，停止
    pause
    exit /b 1
)

echo.
echo ====== Step 2/2: 匯出乾淨資料包 ======
echo.
"%PY%" export_institutional.py
if errorlevel 1 (
    echo.
    echo [!] 匯出失敗
    pause
    exit /b 1
)

echo.
echo ====== 全部完成 ======
echo 資料包位置: %~dp0data\
echo.
pause
