@echo off
cd /d D:\claude\tw-stock-scanner
taskkill /F /IM ngrok.exe 2>nul
"C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe" watchdog.py
