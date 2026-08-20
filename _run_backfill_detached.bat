@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
title TW-Backfill-Institutional
"%PY%" backfill_institutional.py
