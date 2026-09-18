@echo off
cd /d "%~dp0"
title PharmaLink
echo.
echo PharmaLink — XAMPP is not required. Database is Supabase.
echo Folder: %CD%
echo When you see "Uvicorn running", open: http://127.0.0.1:8080
echo Press Ctrl+C in this window to stop.
echo.

py -3.12 -m uvicorn app.main:app --host 127.0.0.1 --port 8080
if errorlevel 1 (
  echo Python 3.12 launcher failed, trying Program Files...
  "C:\Program Files\Python312\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8080
)

echo.
pause
