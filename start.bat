@echo off
cd /d D:\claude\tw-stock-scanner

:: 先背景補抓缺少的資料
start /b "" "C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe" auto_update.py

:: 啟動網站 + 開瀏覽器
start "" http://127.0.0.1:5000
"C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe" app.py
