@echo off
setlocal
cd /d "%~dp0"

:: 1. Check if server is already running on port 5000
netstat -ano | findstr :5000 | findstr LISTENING >nul
if %errorlevel% neq 0 (
    echo Starting Journal Guard Server...
    start /min "Journal Guard Server" cmd /c "python app.py"
    ping 127.0.0.1 -n 3 >nul
)

:: 2. Launch in dedicated standalone Desktop App Window
set "APP_URL=http://localhost:5000"

where msedge >nul 2>&1
if %errorlevel% equ 0 (
    start "" msedge --app=%APP_URL%
    exit /b
)

where chrome >nul 2>&1
if %errorlevel% equ 0 (
    start "" chrome --app=%APP_URL%
    exit /b
)

start "" %APP_URL%
exit /b
