@echo off
setlocal enabledelayedexpansion
title GitInTheVan Auto-Update
cd /d "%~dp0\.."
set "GITV_ROOT=%CD%"
set "LOG_FILE=%GITV_ROOT%\data\updater.log"
set "ZIP_FILE=%GITV_ROOT%\data\gitinthevan.zip"
set "CHAIN_LOG=%GITV_ROOT%\data\update-chain.log"

REM Arguments are optional and order-independent so that a NEW app version can
REM drive an OLD copy of this script (which happens on the first hop of every
REM upgrade) without the extra arguments breaking anything.
REM   --auto        unattended; never wait for a keypress
REM   <port>        port the server listens on (default 8000)
set "GITV_PORT=8000"
set "GITV_AUTO=0"
for %%a in (%*) do (
    if /I "%%a"=="--auto" (
        set "GITV_AUTO=1"
    ) else (
        echo %%a| findstr /R "^[0-9][0-9]*$" >nul && set "GITV_PORT=%%a"
    )
)

REM Rotate the previous run's log instead of truncating it. A chained upgrade
REM runs this script once per hop, and without rotation each hop destroys the
REM evidence needed to diagnose the one before it.
if not exist "%GITV_ROOT%\data\update-logs" mkdir "%GITV_ROOT%\data\update-logs"
if exist "%LOG_FILE%" (
    set "ROT=%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
    set "ROT=!ROT: =0!"
    move /Y "%LOG_FILE%" "%GITV_ROOT%\data\update-logs\updater-!ROT!.log" >nul 2>&1
    for /f "skip=10 delims=" %%f in ('dir /b /o-d "%GITV_ROOT%\data\update-logs\updater-*.log" 2^>nul') do (
        del "%GITV_ROOT%\data\update-logs\%%f" >nul 2>&1
    )
)

echo ============================================ > "%LOG_FILE%"
echo   GitInTheVan Auto-Update Log >> "%LOG_FILE%"
echo   Date: %DATE% %TIME% >> "%LOG_FILE%"
echo   Script: %~dp0 >> "%LOG_FILE%"
echo ============================================ >> "%LOG_FILE%"

echo ============================================
echo   GitInTheVan - Auto-Update
echo ============================================
echo.

REM 3-second delay to let the HTTP response return (ping used instead of timeout for no-console compatibility)
ping -n 4 127.0.0.1 >nul

REM ============================================================
REM Stop running server
REM ============================================================
echo [1/6] Stopping server if running...
echo [1/6] Stopping server... >> "%LOG_FILE%"
"%GITV_ROOT%\.venv\Scripts\python" -c "import socket,sys; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1',int(sys.argv[1]))); s.close(); exit(0 if r==0 else 1)" !GITV_PORT! >nul 2>&1
if not errorlevel 1 (
    echo Server is running on port !GITV_PORT!. Stopping... >> "%LOG_FILE%"
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":!GITV_PORT!.*LISTENING"') do (
        taskkill /PID %%a /F >nul 2>&1
        echo Killed PID %%a >> "%LOG_FILE%"
    )
    timeout /t 2 /nobreak >nul
) else (
    echo No server detected on port !GITV_PORT!. >> "%LOG_FILE%"
)
echo Done.
echo.

REM ============================================================
REM Start maintenance page (served until the real server restarts)
REM ============================================================
set "MAINT_SCRIPT=%GITV_ROOT%\data\_maintenance_server.py"
if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" goto :skip_maintenance_page

REM Delayed expansion (enabled at the top of this script) mangles "!" in
REM literal text (e.g. "<!doctype html>"), so it is disabled for this block.
REM Outside of an if/for block, parens don't need ^ escaping - only the
REM always-special redirection characters < and > do.
setlocal disabledelayedexpansion
> "%MAINT_SCRIPT%" echo import http.server as hs
>> "%MAINT_SCRIPT%" echo import socketserver as ss
>> "%MAINT_SCRIPT%" echo PAGE = b'^<!doctype html^>^<html^>^<head^>^<meta charset="utf-8"^>^<meta http-equiv="refresh" content="10"^>^<title^>GitInTheVan - Updating^</title^>^<style^>body{font-family:sans-serif;text-align:center;padding-top:15vh;background:#111;color:#eee}^</style^>^</head^>^<body^>^<h1^>GitInTheVan is updating^</h1^>^<p^>This page will refresh automatically.^</p^>^</body^>^</html^>'
>> "%MAINT_SCRIPT%" echo Handler = type('Handler', (hs.BaseHTTPRequestHandler,), {})
>> "%MAINT_SCRIPT%" echo def _do_get(self): self.send_response(200); self.send_header('Content-Type', 'text/html'); self.send_header('Content-Length', str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)
>> "%MAINT_SCRIPT%" echo Handler.do_GET = _do_get
>> "%MAINT_SCRIPT%" echo Handler.log_message = lambda self, *a: None
>> "%MAINT_SCRIPT%" echo Server = type('Server', (ss.TCPServer,), {'allow_reuse_address': True})
>> "%MAINT_SCRIPT%" echo import os
>> "%MAINT_SCRIPT%" echo httpd = Server(('0.0.0.0', int(os.environ.get('GITV_MAINT_PORT', '8000'))), Handler)
>> "%MAINT_SCRIPT%" echo httpd.serve_forever()
endlocal

if exist "%MAINT_SCRIPT%" (
    set "GITV_MAINT_PORT=!GITV_PORT!"
    start "" /b "%GITV_ROOT%\.venv\Scripts\python.exe" "%MAINT_SCRIPT%"
    echo Maintenance page serving on port !GITV_PORT! during update. >> "%LOG_FILE%"
) else (
    echo WARNING: Failed to write maintenance page script. >> "%LOG_FILE%"
)
:skip_maintenance_page
echo.

REM ============================================================
REM Backup database
REM ============================================================
echo [2/6] Backing up database...
echo [2/6] Backing up database... >> "%LOG_FILE%"
if exist "%GITV_ROOT%\data\gitinthevan.db" (
    REM Seconds included: two hops of a chain can land in the same minute, and
    REM /Y because an unattended `copy` onto an existing target prompts, which
    REM hangs forever with stdin detached.
    set "BACKUP_NAME=data\gitinthevan_backup_%date:~-4,4%%date:~-10,2%%date:~-7,2%_%time:~0,2%%time:~3,2%%time:~6,2%.db"
    set "BACKUP_NAME=!BACKUP_NAME: =0!"
    copy /Y "%GITV_ROOT%\data\gitinthevan.db" "%GITV_ROOT%\!BACKUP_NAME!" >nul
    echo Database backed up to !BACKUP_NAME! >> "%LOG_FILE%"
    echo Database backed up to !BACKUP_NAME!
    REM Prune to the newest 10. Top level of data\ only: data\backups\ uses the
    REM same filename prefix for scheduled backups, managed elsewhere.
    for /f "skip=10 delims=" %%f in ('dir /b /o-d "%GITV_ROOT%\data\gitinthevan_backup_*.db" 2^>nul') do (
        del "%GITV_ROOT%\data\%%f" >nul 2>&1
    )
) else (
    echo No database found at data\gitinthevan.db >> "%LOG_FILE%"
    echo No database found, skipping backup.
)
echo.

REM ============================================================
REM Extract zip if present
REM ============================================================
echo [3/6] Extracting update zip...
echo [3/6] Extracting update zip... >> "%LOG_FILE%"

if exist "%ZIP_FILE%" (
    echo Found %ZIP_FILE% >> "%LOG_FILE%"

    REM Detect PowerShell
    set "PS_CMD="
    where powershell >nul 2>&1
    if not errorlevel 1 (
        set "PS_CMD=powershell"
    ) else if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
        set "PS_CMD=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    )

    if not defined PS_CMD (
        echo ERROR: PowerShell not found for zip extraction. >> "%LOG_FILE%"
        echo ERROR: PowerShell not found for zip extraction. >> "%CHAIN_LOG%"
        echo ERROR: PowerShell not found. Cannot extract zip.
        if "!GITV_AUTO!"=="0" pause
        exit /b 1
    )

    REM Extract to temp folder, then copy over
    set "EXTRACT_TEMP=%GITV_ROOT%\data\_update_extract"
    if exist "!EXTRACT_TEMP!" rmdir /s /q "!EXTRACT_TEMP!"
    mkdir "!EXTRACT_TEMP!"

    echo Extracting zip to temp... >> "%LOG_FILE%"
    "!PS_CMD!" -Command "$ProgressPreference = 'SilentlyContinue'; Expand-Archive -Path '%ZIP_FILE%' -DestinationPath '!EXTRACT_TEMP!' -Force" >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo ERROR: Zip extraction failed. >> "%LOG_FILE%"
        echo ERROR: Zip extraction failed. >> "%CHAIN_LOG%"
        echo ERROR: Failed to extract zip.
        if "!GITV_AUTO!"=="0" pause
        exit /b 1
    )

    REM Check if extraction produced a single top-level folder (GitHub zipball format)
    set "TOP_DIR="
    for /d %%d in ("!EXTRACT_TEMP!\*") do (
        if "!TOP_DIR!"=="" (
            set "TOP_DIR=%%d"
        ) else (
            set "TOP_DIR=MULTIPLE"
        )
    )

    REM If single top-level folder, use its contents
    if not "!TOP_DIR!"=="" if not "!TOP_DIR!"=="MULTIPLE" (
        echo Found nested folder, copying contents... >> "%LOG_FILE%"
        xcopy "!TOP_DIR!\*" "%GITV_ROOT%\" /E /Y /Q >> "%LOG_FILE%" 2>&1
    ) else (
        echo Copying extracted files to root... >> "%LOG_FILE%"
        xcopy "!EXTRACT_TEMP!\*" "%GITV_ROOT%\" /E /Y /Q >> "%LOG_FILE%" 2>&1
    )

    rmdir /s /q "!EXTRACT_TEMP!" 2>nul
    del "%ZIP_FILE%" >nul 2>&1
    echo Update files extracted. >> "%LOG_FILE%"
    echo Done.
) else (
    echo No zip file found at %ZIP_FILE% >> "%LOG_FILE%"
    echo No update zip found. Running reinstall only.
)
echo.

REM ============================================================
REM Reinstall dependencies
REM ============================================================
echo [4/6] Reinstalling Python dependencies...
echo [4/6] Reinstalling Python dependencies... >> "%LOG_FILE%"
if exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    REM Pinned like every other dependency (exact pins only, never ranges) - an unpinned
    REM `--upgrade pip` is an unreviewed network fetch on every single update.
    "%GITV_ROOT%\.venv\Scripts\python" -m pip install "pip==26.2" -q >> "%LOG_FILE%" 2>&1
    "%GITV_ROOT%\.venv\Scripts\pip" install -e "%GITV_ROOT%[dev]" -q >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo WARNING: Some dependencies may not have installed correctly. >> "%LOG_FILE%"
        echo WARNING: Dependency installation had errors.
    ) else (
        echo Dependencies installed. >> "%LOG_FILE%"
    )
) else (
    echo ERROR: Python venv not found. Run the full deploy script first. >> "%LOG_FILE%"
    echo ERROR: Python venv not found. Run the full deploy script first. >> "%CHAIN_LOG%"
    echo ERROR: Python venv not found. Run deploy-windows.bat first.
    REM `pause` under CREATE_NEW_CONSOLE with stdin detached blocks forever, with
    REM the maintenance page already dead. Only wait when a human is watching.
    if "!GITV_AUTO!"=="0" pause
    exit /b 1
)
echo Done.
echo.

REM ============================================================
REM Rebuild frontend
REM ============================================================
echo [5/6] Rebuilding frontend...
echo [5/6] Rebuilding frontend... >> "%LOG_FILE%"
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
    goto :start_server
)

echo Using Node: !NODE_CMD! >> "%LOG_FILE%"
cd /d "%GITV_ROOT%\frontend"
REM `npm ci` installs strictly from package-lock.json. `npm install` would
REM re-resolve against the live registry and rewrite the lockfile, which
REM defeats the exact pinning required by the dependency pinning policy. On failure keep
REM the existing static\ build rather than falling back to `npm install`.
call "!NPM_CMD!" ci -q >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: npm ci failed ^(package.json/package-lock.json may disagree^). Using existing frontend build. >> "%LOG_FILE%"
    echo WARNING: npm ci failed. Using existing frontend build.
    goto :frontend_done
)
call "!NPM_CMD!" run build >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Frontend build failed. Using existing build. >> "%LOG_FILE%"
    echo WARNING: Frontend build failed. Using existing build.
) else (
    echo Frontend built successfully. >> "%LOG_FILE%"
)

:frontend_done
cd /d "%GITV_ROOT%"
echo Done.
echo.

REM ============================================================
REM Start server
REM ============================================================
:start_server
echo [6/6] Starting GitInTheVan...
echo [6/6] Starting server... >> "%LOG_FILE%"
echo.
echo ============================================
echo   Update complete! Starting server...
echo ============================================
echo.

cd /d "%GITV_ROOT%"

REM Stop the maintenance page so the real server can bind the port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":!GITV_PORT!.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
del "%MAINT_SCRIPT%" >nul 2>&1

REM Start server in a new process, then clean up this script.
REM
REM Do NOT delete data\update-chain.json here. It carries the frozen
REM multi-release upgrade plan across restarts, and the newly started server
REM reads it to decide whether another hop is due. data\ is gitignored and
REM absent from the release zip, which is why chain state lives there and
REM survives extraction.
start "" "%GITV_ROOT%\.venv\Scripts\python" -m app.main
del "%GITV_ROOT%\data\auto-update.bat" >nul 2>&1
exit
