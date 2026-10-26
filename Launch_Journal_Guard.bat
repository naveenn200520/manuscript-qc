@echo off
title Journal Guard — Manuscript & Artwork QC Platform
color 0A
cls
echo =====================================================================
echo              JOURNAL GUARD - PRODUCTION QC PLATFORM
echo =====================================================================
echo.
echo [1/3] Checking environment...
cd /d "%~dp0"

echo [2/3] Checking if Journal Guard is already running on port 5000...
netstat -ano | findstr :5000 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo.
    echo [*] Journal Guard is already running! Opening your browser...
    start http://localhost:5000
    timeout /t 3 >nul
    exit /b
)

echo.
echo [3/3] Starting Journal Guard Server...
start "" http://localhost:5000
python app.py

pause
