@echo off
rem ============================================================
rem  MyAi one-click launcher
rem   - backend : D:\python3.11\python.exe -m uvicorn backend.main:app --port 8000
rem   - frontend: npm run dev (Vite, http://localhost:5173)
rem   - browser : opened automatically after the frontend is ready
rem   - stop    : press Ctrl+C or close this window (both services are killed)
rem ============================================================
setlocal
cd /d "%~dp0"
title MyAi Launcher

if not exist "%~dp0scripts\start.ps1" (
  echo [Nexus] scripts\start.ps1 not found. Is the project complete?
  pause
  exit /b 1
)

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" (
  echo [Nexus] Windows PowerShell was not found.
  pause
  exit /b 1
)

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
set "CODE=%errorlevel%"

if not "%CODE%"=="0" (
  echo.
  echo [Nexus] launcher exited with code %CODE%.
  pause
)
exit /b %CODE%