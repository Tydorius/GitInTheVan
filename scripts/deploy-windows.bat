@echo off
setlocal enabledelayedexpansion
title GitInTheVan Deploy

cd /d "%~dp0\.."
set "GITV_ROOT=%CD%"
set "LOG_FILE=%~dp0installer.log"

echo ============================================
echo   GitInTheVan - Windows Deploy
echo ============================================
echo Installer log: %LOG_FILE%
echo.

(
    echo ============================================
    echo   GitInTheVan Deploy Log
    echo   Date: %DATE% %TIME%
    echo ============================================
    echo.
    echo Working directory: %GITV_ROOT%
    echo Script location: %~dp0
    echo Changed to: %GITV_ROOT%
    echo.

    REM ============================================================
    REM Check Python version (3.12+ required)
    REM ============================================================
    echo [1/6] Checking Python...
    set "PYTHON_CMD="
    set PYOK=0

    python --version >nul 2>&1
    if errorlevel 1 goto :python_not_found

    python --version
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
        set PYMAJOR=%%a
        set PYMINOR=%%b
    )
    echo DEBUG: PYVER=!PYVER! PYMAJOR=!PYMAJOR! PYMINOR=!PYMINOR!

    if !PYMAJOR! GTR 3 goto :python_found_default
    if !PYMAJOR! EQU 3 if !PYMINOR! GEQ 12 goto :python_found_default

    echo.
    echo Python !PYMAJOR!.!PYMINOR! found but 3.12+ required.
    echo Searching for newer Python installations...

    py -3.12 --version >nul 2>&1
    if not errorlevel 1 (
        echo Found Python 3.12 via py launcher.
        for /f "delims=" %%p in ('py -3.12 -c "import sys; print(sys.executable)" 2^>^&1') do set "PYTHON_CMD=%%p"
        goto :python_done
    )
    py -3.13 --version >nul 2>&1
    if not errorlevel 1 (
        echo Found Python 3.13 via py launcher.
        for /f "delims=" %%p in ('py -3.13 -c "import sys; print(sys.executable)" 2^>^&1') do set "PYTHON_CMD=%%p"
        goto :python_done
    )

    for %%P in (312 313 314) do (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe" (
            set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe"
            echo Found Python at !PYTHON_CMD!
            goto :python_done
        )
    )

    for %%P in (312 313 314) do (
        if exist "C:\Program Files\Python%%P\python.exe" (
            set "PYTHON_CMD=C:\Program Files\Python%%P\python.exe"
            echo Found Python at !PYTHON_CMD!
            goto :python_done
        )
        if exist "C:\Program Files (x86)\Python%%P\python.exe" (
            set "PYTHON_CMD=C:\Program Files (x86)\Python%%P\python.exe"
            echo Found Python at !PYTHON_CMD!
            goto :python_done
        )
    )

    goto :python_offer_install

    :python_not_found
    echo Python not found in PATH.

    :python_offer_install
    echo.
    where winget >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python 3.12+ is required but winget is not available.
        echo Please install Python 3.12+ from https://python.org
        pause
        goto :write_log_exit
    )

    echo Python 3.12+ is required.
    set "INSTALL_PY="
    set /p INSTALL_PY="Would you like to install Python 3.12 via winget? [y/n]: "
    if /i not "!INSTALL_PY!"=="y" (
        echo Please install Python 3.12+ from https://python.org
        pause
        goto :write_log_exit
    )

    echo.
    echo Installing Python 3.12 via winget...
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo ERROR: winget installation failed.
        echo Please install Python 3.12+ manually from https://python.org
        pause
        goto :write_log_exit
    )

    echo Searching for installed Python...
    py -3.12 --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('py -3.12 -c "import sys; print(sys.executable)" 2^>^&1') do set "PYTHON_CMD=%%p"
        goto :python_done
    )

    for %%P in (312 313) do (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe" (
            set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe"
            goto :python_done
        )
        if exist "C:\Program Files\Python%%P\python.exe" (
            set "PYTHON_CMD=C:\Program Files\Python%%P\python.exe"
            goto :python_done
        )
    )

    echo Python installed but not found in expected locations.
    echo Please close this window, open a new command prompt, and re-run this script.
    pause
    goto :write_log_exit

    :python_found_default
    set "PYTHON_CMD=python"
    echo DEBUG: Using default python from PATH

    :python_done
    echo DEBUG: PYTHON_CMD=[!PYTHON_CMD!]
    if "!PYTHON_CMD!"=="" (
        echo ERROR: PYTHON_CMD is empty. Python was not detected properly.
        echo Please report this error with the debug output above.
        pause
        goto :write_log_exit
    )
    echo.

    REM ============================================================
    REM Set up Python environment
    REM ============================================================
    echo [2/6] Setting up Python environment...
    if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
        echo Creating virtual environment...
        echo DEBUG: Running: "!PYTHON_CMD!" -m venv "%GITV_ROOT%\.venv"
        "!PYTHON_CMD!" -m venv "%GITV_ROOT%\.venv"
        if errorlevel 1 (
            echo ERROR: Failed to create virtual environment.
            echo DEBUG: PYTHON_CMD was [!PYTHON_CMD!]
            pause
            goto :write_log_exit
        )
    ) else (
        echo DEBUG: .venv already exists, skipping creation
    )
    echo Upgrading pip...
    "%GITV_ROOT%\.venv\Scripts\python" -m pip install --upgrade pip -q
    echo Installing dependencies...
    "%GITV_ROOT%\.venv\Scripts\pip" install -e "%GITV_ROOT%[dev]" -q
    if errorlevel 1 (
        echo ERROR: Failed to install Python dependencies.
        echo This usually means pip is outdated or Python version is incompatible.
        pause
        goto :write_log_exit
    )
    echo Done.
    echo.

    REM ============================================================
    REM Check Deno
    REM ============================================================
    echo [3/6] Checking Deno runtime...
    set "DENO_DIR=%GITV_ROOT%\.deno"
    set "DENO_EXE=%DENO_DIR%\deno.exe"

    if exist "%DENO_EXE%" (
        echo Deno found at %DENO_EXE%
    ) else (
        where deno >nul 2>&1
        if not errorlevel 1 (
            echo Deno found in PATH
        ) else (
            echo Deno not found. Downloading...
            if not exist "%DENO_DIR%" mkdir "%DENO_DIR%"
            set "DENO_ZIP=%DENO_DIR%\deno.zip"
            echo DEBUG: Deno dir: %DENO_DIR%
            echo DEBUG: Download target: !DENO_ZIP!
            powershell -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile '!DENO_ZIP!'"
            if errorlevel 1 (
                echo WARNING: Could not download Deno automatically.
                echo Cantrips will not work. Please install Deno manually from https://deno.land
                echo Or download and place at %DENO_EXE%
            ) else (
                echo DEBUG: Extracting !DENO_ZIP! to %DENO_DIR%
                powershell -Command "Expand-Archive -Path '!DENO_ZIP!' -DestinationPath '%DENO_DIR%' -Force"
                del "!DENO_ZIP!"
                if exist "%DENO_EXE%" (
                    echo Deno installed to %DENO_EXE%
                ) else (
                    echo WARNING: Deno download succeeded but deno.exe not found at expected location.
                    echo DEBUG: Contents of %DENO_DIR%:
                    dir "%DENO_DIR%" /b
                    echo.
                    echo Cantrips will not work without Deno.
                )
            )
        )
    )
    echo.

    REM ============================================================
    REM Check Node.js and build frontend
    REM ============================================================
    echo [4/6] Checking Node.js and building frontend...
    set "NODE_CMD="
    set "NPM_CMD="

    where node >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
        echo Found Node.js in PATH: !NODE_CMD!
        where npm >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
        )
        goto :node_found
    )

    echo Node.js not found in PATH. Searching common locations...

    if exist "C:\Program Files\nodejs\node.exe" (
        set "NODE_CMD=C:\Program Files\nodejs\node.exe"
        set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
        echo Found Node.js at !NODE_CMD!
        goto :node_found
    )

    if exist "C:\Program Files (x86)\nodejs\node.exe" (
        set "NODE_CMD=C:\Program Files (x86)\nodejs\node.exe"
        set "NPM_CMD=C:\Program Files (x86)\nodejs\npm.cmd"
        echo Found Node.js at !NODE_CMD!
        goto :node_found
    )

    if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" (
        set "NODE_CMD=%LOCALAPPDATA%\Programs\nodejs\node.exe"
        set "NPM_CMD=%LOCALAPPDATA%\Programs\nodejs\npm.cmd"
        echo Found Node.js at !NODE_CMD!
        goto :node_found
    )

    for %%v in (24 23 22 21 20) do (
        for /f "delims=" %%p in ('dir /b /ad "%LOCALAPPDATA%\nvm\v%%v*" 2^>nul') do (
            if exist "%LOCALAPPDATA%\nvm\%%p\node.exe" (
                set "NODE_CMD=%LOCALAPPDATA%\nvm\%%p\node.exe"
                set "NPM_CMD=%LOCALAPPDATA%\nvm\%%p\npm.cmd"
                echo Found Node.js via nvm-windows at !NODE_CMD!
                goto :node_found
            )
        )
    )

    if exist "%LOCALAPPDATA%\fnm_multishells" (
        for /f "delims=" %%p in ('dir /b /ad /s "%LOCALAPPDATA%\fnm_multishells\*\node.exe" 2^>nul') do (
            set "NODE_CMD=%%p"
            echo Found Node.js via fnm at !NODE_CMD!
            goto :node_found
        )
    )

    echo DEBUG: Node.js not found in any location.

    where winget >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Node.js 24+ is required to build the web UI.
        echo winget is not available, so automatic installation is not possible.
        goto :node_failed
    )

    echo.
    echo Node.js 24+ is required to build the web UI.
    set "INSTALL_NODE="
    set /p INSTALL_NODE="Would you like to install Node.js via winget? [y/n]: "
    if /i not "!INSTALL_NODE!"=="y" goto :node_failed

    echo.
    echo Installing Node.js LTS via winget...
    winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo ERROR: winget installation of Node.js failed.
        goto :node_failed
    )

    where node >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
        where npm >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
        )
        echo Found Node.js after install: !NODE_CMD!
        goto :node_found
    )
    if exist "C:\Program Files\nodejs\node.exe" (
        set "NODE_CMD=C:\Program Files\nodejs\node.exe"
        set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
        goto :node_found
    )

    echo Node.js installed but not found. Please close this window, open a new
    echo command prompt, and re-run this script.
    goto :node_failed

    :node_failed
    if not exist "%GITV_ROOT%\static\index.html" (
        echo.
        echo ============================================
        echo ERROR: Cannot start without a frontend build.
        echo ============================================
        echo Node.js is required to build the web UI.
        echo.
        echo Options:
        echo   1. Install Node.js 24+ from https://nodejs.org
        echo   2. Close this window, open a new command prompt, and re-run this script.
        echo.
        pause
        goto :write_log_exit
    ) else (
        echo WARNING: Node.js not found. Using existing frontend build.
        echo To update the UI after upgrades, install Node.js 24+ from https://nodejs.org
        goto :frontend_done
    )

    :node_found
    echo DEBUG: NODE_CMD=[!NODE_CMD!]
    echo DEBUG: NPM_CMD=[!NPM_CMD!]
    echo Node.js version:
    "!NODE_CMD!" --version

    "!NODE_CMD!" -e "process.exit(process.versions.node >= 18 ? 0 : 1)" 2>nul
    if errorlevel 1 (
        echo WARNING: Node.js version is too old (18+ required, 24+ recommended).
        echo Current version:
        "!NODE_CMD!" --version
        echo.
        if exist "%GITV_ROOT%\static\index.html" (
            echo Using existing frontend build. Upgrade Node.js to update the UI.
            goto :frontend_done
        ) else (
            echo ERROR: Cannot build frontend with this Node.js version.
            echo Please install Node.js 24+ from https://nodejs.org
            pause
            goto :write_log_exit
        )
    )

    echo.
    echo Building frontend...
    cd /d "%GITV_ROOT%\frontend"
    if not exist "node_modules" (
        echo Installing frontend dependencies...
        call "!NPM_CMD!" install -q
    ) else (
        echo Updating frontend dependencies...
        call "!NPM_CMD!" install -q
    )
    echo Building frontend...
    call "!NPM_CMD!" run build
    if errorlevel 1 (
        echo ERROR: Frontend build failed.
        cd /d "%GITV_ROOT%"
        if not exist "%GITV_ROOT%\static\index.html" (
            echo Cannot start without a frontend build.
            pause
            goto :write_log_exit
        )
        echo WARNING: Build failed but existing frontend found. Continuing with old build.
    ) else (
        echo Frontend built successfully.
    )
    cd /d "%GITV_ROOT%"

    :frontend_done
    echo.

    REM ============================================================
    REM Configuration
    REM ============================================================
    echo [5/6] Checking configuration...
    if not exist "%GITV_ROOT%\.env" (
        echo Creating .env from template...
        copy "%GITV_ROOT%\.env.example" "%GITV_ROOT%\.env" >nul
        echo Created .env - edit it to configure your endpoint and secret key.
    )
    echo.

    if not exist "%GITV_ROOT%\data" mkdir "%GITV_ROOT%\data"

    REM ============================================================
    REM Verify installation
    REM ============================================================
    echo Verifying installation...
    set VERIFY_OK=1
    if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
        echo ERROR: Python venv not found at %GITV_ROOT%\.venv\Scripts\python.exe
        set VERIFY_OK=0
    )
    if not exist "%GITV_ROOT%\static\index.html" (
        echo ERROR: Frontend build not found at %GITV_ROOT%\static\index.html
        echo The web UI will not load. Ensure Node.js 24+ is installed and re-run.
        set VERIFY_OK=0
    )
    if not exist "%DENO_EXE%" (
        where deno >nul 2>&1
        if errorlevel 1 (
            echo WARNING: Deno not found at %DENO_EXE% or in PATH
            echo Cantrips will not work without Deno.
            set VERIFY_OK=0
        )
    )
    if "!VERIFY_OK!"=="0" (
        echo.
        echo ERROR: Installation verification failed. See errors above.
        pause
        goto :write_log_exit
    )
    echo All components verified.

    REM ============================================================
    REM Configure Windows Firewall
    REM ============================================================
    echo.
    echo Configuring Windows Firewall...
    set "FIREWALL_RULE_NAME=GitInTheVan"
    netsh advfirewall firewall show rule name="!FIREWALL_RULE_NAME!" >nul 2>&1
    if errorlevel 1 (
        echo Creating firewall rule for port 8000...
        netsh advfirewall firewall add rule name="!FIREWALL_RULE_NAME!" dir=in action=allow protocol=TCP localport=8000 >nul 2>&1
        if errorlevel 1 (
            echo WARNING: Could not create firewall rule automatically.
            echo If you cannot connect from other devices, run this as administrator:
            echo   netsh advfirewall firewall add rule name="GitInTheVan" dir=in action=allow protocol=TCP localport=8000
        ) else (
            echo Firewall rule created. Port 8000 is open for inbound connections.
        )
    ) else (
        echo Firewall rule already exists.
    )

    REM ============================================================
    REM Start server
    REM ============================================================
    echo [6/6] Starting GitInTheVan...
    echo.
    echo ============================================
    echo   GitInTheVan is starting...
    echo   Web UI: http://localhost:8000
    echo   (or http://127.0.0.1:8000)
    echo   Press Ctrl+C to stop.
    echo ============================================
    echo.

    echo [INSTALL COMPLETE - Server starting] >> "%LOG_FILE%"
    cd /d "%GITV_ROOT%"
    "%GITV_ROOT%\.venv\Scripts\uvicorn" app.main:app --host :: --port 8000
    pause

    :write_log_exit
) > "%LOG_FILE%" 2>&1

REM Display the log on screen
type "%LOG_FILE%"
echo.
echo Installer log saved to: %LOG_FILE%
echo.
pause
