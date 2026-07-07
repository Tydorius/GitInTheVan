@echo off
setlocal enabledelayedexpansion
title GitInTheVan Update
cd /d "%~dp0\.."
set "GITV_ROOT=%CD%"
set "LOG_FILE=%~dp0updater.log"

echo ============================================ > "%LOG_FILE%"
echo   GitInTheVan Update Log >> "%LOG_FILE%"
echo   Date: %DATE% %TIME% >> "%LOG_FILE%"
echo ============================================ >> "%LOG_FILE%"

echo ============================================
echo   GitInTheVan - Windows Update
echo ============================================
echo.

REM ============================================================
REM Detect PowerShell
REM ============================================================
set "PS_CMD="
where powershell >nul 2>&1
if not errorlevel 1 (
    set "PS_CMD=powershell"
) else if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    set "PS_CMD=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
)

REM ============================================================
REM Stop running server
REM ============================================================
echo [1/5] Stopping server if running...
echo [1/5] Stopping server... >> "%LOG_FILE%"
"%GITV_ROOT%\.venv\Scripts\python" -c "import socket; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1',8000)); s.close(); exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
    echo Server is running on port 8000. Attempting to stop...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000.*LISTENING"') do (
        taskkill /PID %%a /F >nul 2>&1
        echo Killed PID %%a >> "%LOG_FILE%"
    )
    timeout /t 2 /nobreak >nul
) else (
    echo No server detected on port 8000. >> "%LOG_FILE%"
)
echo Done.
echo.

REM ============================================================
REM Backup database
REM ============================================================
echo [2/5] Backing up database...
echo [2/5] Backing up database... >> "%LOG_FILE%"
if exist "%GITV_ROOT%\data\gitinthevan.db" (
    set "BACKUP_NAME=data\gitinthevan_backup_%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%.db"
    set "BACKUP_NAME=!BACKUP_NAME: =0!"
    copy "%GITV_ROOT%\data\gitinthevan.db" "%GITV_ROOT%\!BACKUP_NAME!" >nul
    echo Database backed up to !BACKUP_NAME! >> "%LOG_FILE%"
    echo Database backed up to !BACKUP_NAME!
) else (
    echo No database found at data\gitinthevan.db >> "%LOG_FILE%"
    echo No database found, skipping backup.
)
echo.

REM ============================================================
 reinstall dependencies
REM ============================================================
echo [3/5] Reinstalling Python dependencies...
echo [3/5] Reinstalling Python dependencies... >> "%LOG_FILE%"
if exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    "%GITV_ROOT%\.venv\Scripts\python" -m pip install --upgrade pip -q >> "%LOG_FILE%" 2>&1
    "%GITV_ROOT%\.venv\Scripts\pip" install -e "%GITV_ROOT%[dev]" -q >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo WARNING: Some dependencies may not have installed correctly. >> "%LOG_FILE%"
        echo WARNING: Dependency installation had errors.
    ) else (
        echo Dependencies installed. >> "%LOG_FILE%"
    )
) else (
    echo ERROR: Python venv not found. Run the full deploy script first. >> "%LOG_FILE%"
    echo ERROR: Python venv not found. Run deploy-windows.bat first.
    pause
    exit /b 1
)
echo Done.
echo.

REM ============================================================
REM Rebuild frontend
REM ============================================================
echo [4/5] Rebuilding frontend...
echo [4/5] Rebuilding frontend... >> "%LOG_FILE%"
set "NODE_CMD="
set "NPM_CMD="

if exist "%GITV_ROOT%\.node\node.exe" (
    set "NODE_CMD=%GITV_ROOT%\.node\node.exe"
    set "NPM_CMD=%GITV_ROOT%\.node\npm.cmd"
) else (
    where node >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
        where npm >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
        )
    )
    if not defined NODE_CMD (
        if exist "C:\Program Files\nodejs\node.exe" (
            set "NODE_CMD=C:\Program Files\nodejs\node.exe"
            set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
        )
    )
)

if not defined NODE_CMD (
    echo WARNING: Node.js not found, skipping frontend build. >> "%LOG_FILE%"
    echo WARNING: Node.js not found. Using existing frontend build.
    goto :update_done
)

echo Using Node: !NODE_CMD! >> "%LOG_FILE%"
cd /d "%GITV_ROOT%\frontend"
call "!NPM_CMD!" install -q >> "%LOG_FILE%" 2>&1
call "!NPM_CMD!" run build >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Frontend build failed. Using existing build. >> "%LOG_FILE%"
    echo WARNING: Frontend build failed. Using existing build.
) else (
    echo Frontend built successfully. >> "%LOG_FILE%"
)
cd /d "%GITV_ROOT%"
echo Done.
echo.

:update_done
REM ============================================================
REM Start server
REM ============================================================
echo [5/5] Starting GitInTheVan...
echo [5/5] Starting server... >> "%LOG_FILE%"
echo.
echo ============================================
echo   Update complete! Starting server...
echo ============================================
echo.

cd /d "%GITV_ROOT%"
"%GITV_ROOT%\.venv\Scripts\python" -m app.main
pause
