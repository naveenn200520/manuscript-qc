@echo off
title Setting up Journal Guard Desktop App...
cd /d "%~dp0"
cls
echo =====================================================================
echo           JOURNAL GUARD - 1-CLICK DESKTOP APP SETUP
echo =====================================================================
echo.

:: 1. Check Python
echo [1/3] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.10 or higher and check "Add Python to PATH".
    echo Opening Python download page...
    start https://www.python.org/downloads/
    pause
    exit /b
)
echo [*] Python detected!

:: 2. Check and Install required libraries
echo.
echo [2/3] Checking required libraries...
python -c "import flask, werkzeug, mammoth, docx" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Installing required packages, please wait...
    pip install -r requirements.txt --quiet
)
echo [*] Libraries verified!

:: 3. Create Desktop Shortcut and Launch
echo.
echo [3/3] Creating Desktop App Shortcut with custom icon...
powershell -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1"
echo.
echo Launching Journal Guard...
call "%~dp0Launch_Journal_Guard_App.bat"
echo.
echo =====================================================================
echo  SETUP COMPLETE! You can now launch Journal Guard from your Desktop!
echo =====================================================================
ping 127.0.0.1 -n 3 >nul
exit /b
