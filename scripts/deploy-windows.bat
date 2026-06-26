@echo off
setlocal enabledelayedexpansion
title GitInTheVan Deploy

cd /d "%~dp0\.."
set "GITV_ROOT=%CD%"
set "LOG_FILE=%~dp0installer.log"

REM Initialize log file
echo ============================================ > "%LOG_FILE%"
echo   GitInTheVan Deploy Log >> "%LOG_FILE%"
echo   Date: %DATE% %TIME% >> "%LOG_FILE%"
echo   Root: %GITV_ROOT% >> "%LOG_FILE%"
echo ============================================ >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

echo ============================================
echo   GitInTheVan - Windows Deploy
echo ============================================
echo Installer log: %LOG_FILE%
echo.

echo Working directory: %GITV_ROOT% >> "%LOG_FILE%"
echo Script location: %~dp0 >> "%LOG_FILE%"
echo Changed to: %GITV_ROOT%
echo.

REM ============================================================
REM Check Python version (3.12+ required)
REM ============================================================
echo [1/6] Checking Python...
echo [1/6] Checking Python... >> "%LOG_FILE%"
set "PYTHON_CMD="
set PYOK=0

python --version >nul 2>&1
if errorlevel 1 goto :python_not_found

python --version >> "%LOG_FILE%" 2>&1
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
echo DEBUG: PYVER=!PYVER! PYMAJOR=!PYMAJOR! PYMINOR=!PYMINOR! >> "%LOG_FILE%"

if !PYMAJOR! GTR 3 goto :python_found_default
if !PYMAJOR! EQU 3 if !PYMINOR! GEQ 12 goto :python_found_default

echo.
echo Python !PYMAJOR!.!PYMINOR! found but 3.12+ required.
echo Python !PYMAJOR!.!PYMINOR! found but 3.12+ required. >> "%LOG_FILE%"
echo Searching for newer Python installations...

py -3.12 --version >nul 2>&1
if not errorlevel 1 (
    echo Found Python 3.12 via py launcher. >> "%LOG_FILE%"
    for /f "delims=" %%p in ('py -3.12 -c "import sys; print(sys.executable)" 2^>^&1') do set "PYTHON_CMD=%%p"
    goto :python_done
)
py -3.13 --version >nul 2>&1
if not errorlevel 1 (
    echo Found Python 3.13 via py launcher. >> "%LOG_FILE%"
    for /f "delims=" %%p in ('py -3.13 -c "import sys; print(sys.executable)" 2^>^&1') do set "PYTHON_CMD=%%p"
    goto :python_done
)

for %%P in (312 313 314) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe" (
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe"
        echo Found Python at !PYTHON_CMD! >> "%LOG_FILE%"
        goto :python_done
    )
)

for %%P in (312 313 314) do (
    if exist "C:\Program Files\Python%%P\python.exe" (
        set "PYTHON_CMD=C:\Program Files\Python%%P\python.exe"
        echo Found Python at !PYTHON_CMD! >> "%LOG_FILE%"
        goto :python_done
    )
    if exist "C:\Program Files (x86)\Python%%P\python.exe" (
        set "PYTHON_CMD=C:\Program Files (x86)\Python%%P\python.exe"
        echo Found Python at !PYTHON_CMD! >> "%LOG_FILE%"
        goto :python_done
    )
)

goto :python_offer_install

:python_not_found
echo Python not found in PATH. >> "%LOG_FILE%"

:python_offer_install
echo.
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.12+ is required but winget is not available. >> "%LOG_FILE%"
    echo Please install Python 3.12+ from https://python.org
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)

echo Python 3.12+ is required.
set "INSTALL_PY="
set /p INSTALL_PY="Would you like to install Python 3.12 via winget? [y/n]: "
if /i not "!INSTALL_PY!"=="y" (
    echo Python install declined. >> "%LOG_FILE%"
    echo Please install Python 3.12+ from https://python.org
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)

echo.
echo Installing Python 3.12 via winget...
echo Installing Python via winget... >> "%LOG_FILE%"
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: winget installation failed. >> "%LOG_FILE%"
    echo Please install Python 3.12+ manually from https://python.org
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
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

echo Python installed but not found in expected locations. >> "%LOG_FILE%"
echo Please close this window, open a new command prompt, and re-run this script.
echo.
echo Installer log: %LOG_FILE%
pause
exit /b 1

:python_found_default
set "PYTHON_CMD=python"
echo DEBUG: Using default python from PATH >> "%LOG_FILE%"

:python_done
echo DEBUG: PYTHON_CMD=[!PYTHON_CMD!] >> "%LOG_FILE%"
if "!PYTHON_CMD!"=="" (
    echo ERROR: PYTHON_CMD is empty. >> "%LOG_FILE%"
    echo ERROR: PYTHON_CMD is empty. Python was not detected properly.
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)
echo.

REM ============================================================
REM Set up Python environment
REM ============================================================
echo [2/6] Setting up Python environment...
echo [2/6] Setting up Python environment... >> "%LOG_FILE%"
if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    echo Creating virtual environment... >> "%LOG_FILE%"
    echo Creating virtual environment...
    "!PYTHON_CMD!" -m venv "%GITV_ROOT%\.venv" >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment. >> "%LOG_FILE%"
        echo ERROR: Failed to create virtual environment.
        echo.
        echo Installer log: %LOG_FILE%
        pause
        exit /b 1
    )
) else (
    echo DEBUG: .venv already exists >> "%LOG_FILE%"
)
echo Upgrading pip...
"%GITV_ROOT%\.venv\Scripts\python" -m pip install --upgrade pip -q >> "%LOG_FILE%" 2>&1
echo Installing dependencies...
echo Installing Python dependencies... >> "%LOG_FILE%"
"%GITV_ROOT%\.venv\Scripts\pip" install -e "%GITV_ROOT%[dev]" -q >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to install Python dependencies. >> "%LOG_FILE%"
    echo ERROR: Failed to install Python dependencies.
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)
echo Done.
echo.

REM ============================================================
REM Check Deno
REM ============================================================
echo [3/6] Checking Deno runtime...
echo [3/6] Checking Deno runtime... >> "%LOG_FILE%"
set "DENO_DIR=%GITV_ROOT%\.deno"
set "DENO_EXE=%DENO_DIR%\deno.exe"

if exist "%DENO_EXE%" (
    echo Deno found at %DENO_EXE% >> "%LOG_FILE%"
) else (
    where deno >nul 2>&1
    if not errorlevel 1 (
        echo Deno found in PATH >> "%LOG_FILE%"
    ) else (
        echo Deno not found. Downloading... >> "%LOG_FILE%"
        if not exist "%DENO_DIR%" mkdir "%DENO_DIR%"
        set "DENO_ZIP=%DENO_DIR%\deno.zip"
        powershell -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile '!DENO_ZIP!'" >> "%LOG_FILE%" 2>&1
        if errorlevel 1 (
            echo WARNING: Could not download Deno. >> "%LOG_FILE%"
        ) else (
            powershell -Command "Expand-Archive -Path '!DENO_ZIP!' -DestinationPath '%DENO_DIR%' -Force" >> "%LOG_FILE%" 2>&1
            del "!DENO_ZIP!"
            if exist "%DENO_EXE%" (
                echo Deno installed to %DENO_EXE% >> "%LOG_FILE%"
            ) else (
                echo WARNING: Deno exe not found after download. >> "%LOG_FILE%"
                dir "%DENO_DIR%" /b >> "%LOG_FILE%" 2>&1
            )
        )
    )
)
echo.

REM ============================================================
REM Check Node.js and build frontend
REM ============================================================
echo [4/6] Checking Node.js and building frontend...
echo [4/6] Checking Node.js and building frontend... >> "%LOG_FILE%"
set "NODE_CMD="
set "NPM_CMD="

where node >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
    where npm >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
    )
    echo Found Node.js in PATH: !NODE_CMD! >> "%LOG_FILE%"
    goto :node_found
)

echo Node.js not found in PATH. Searching... >> "%LOG_FILE%"

if exist "C:\Program Files\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
    goto :node_found
)
if exist "C:\Program Files (x86)\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files (x86)\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files (x86)\nodejs\npm.cmd"
    goto :node_found
)
if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" (
    set "NODE_CMD=%LOCALAPPDATA%\Programs\nodejs\node.exe"
    set "NPM_CMD=%LOCALAPPDATA%\Programs\nodejs\npm.cmd"
    goto :node_found
)

for %%v in (24 23 22 21 20) do (
    for /f "delims=" %%p in ('dir /b /ad "%LOCALAPPDATA%\nvm\v%%v*" 2^>nul') do (
        if exist "%LOCALAPPDATA%\nvm\%%p\node.exe" (
            set "NODE_CMD=%LOCALAPPDATA%\nvm\%%p\node.exe"
            set "NPM_CMD=%LOCALAPPDATA%\nvm\%%p\npm.cmd"
            echo Found Node.js via nvm-windows >> "%LOG_FILE%"
            goto :node_found
        )
    )
)

if exist "%LOCALAPPDATA%\fnm_multishells" (
    for /f "delims=" %%p in ('dir /b /ad /s "%LOCALAPPDATA%\fnm_multishells\*\node.exe" 2^>nul') do (
        set "NODE_CMD=%%p"
        echo Found Node.js via fnm >> "%LOG_FILE%"
        goto :node_found
    )
)

echo DEBUG: Node.js not found in any location. >> "%LOG_FILE%"

where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js not found and winget unavailable. >> "%LOG_FILE%"
    goto :node_failed
)

echo.
echo Node.js 24+ is required to build the web UI.
set "INSTALL_NODE="
set /p INSTALL_NODE="Would you like to install Node.js via winget? [y/n]: "
if /i not "!INSTALL_NODE!"=="y" goto :node_failed

echo.
echo Installing Node.js LTS via winget...
echo Installing Node.js via winget... >> "%LOG_FILE%"
winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: winget Node.js install failed. >> "%LOG_FILE%"
    goto :node_failed
)

where node >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
    where npm >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
    )
    goto :node_found
)
if exist "C:\Program Files\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
    goto :node_found
)

echo Node.js installed but not found on PATH. >> "%LOG_FILE%"
goto :node_failed

:node_failed
if not exist "%GITV_ROOT%\static\index.html" (
    echo ============================================
    echo ERROR: Cannot start without a frontend build.
    echo Node.js is required.
    echo ============================================
    echo ERROR: No Node.js and no existing frontend build. >> "%LOG_FILE%"
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
) else (
    echo WARNING: Node.js not found. Using existing frontend build. >> "%LOG_FILE%"
    goto :frontend_done
)

:node_found
echo DEBUG: NODE_CMD=[!NODE_CMD!] >> "%LOG_FILE%"
echo Node.js version: >> "%LOG_FILE%"
"!NODE_CMD!" --version >> "%LOG_FILE%" 2>&1

"!NODE_CMD!" -e "process.exit(process.versions.node >= 18 ? 0 : 1)" 2>nul
if errorlevel 1 (
    echo WARNING: Node.js version too old ^(18+ required^). >> "%LOG_FILE%"
    if exist "%GITV_ROOT%\static\index.html" (
        echo Using existing frontend build. >> "%LOG_FILE%"
        goto :frontend_done
    ) else (
        echo ERROR: Cannot build frontend with old Node.js. >> "%LOG_FILE%"
        echo ERROR: Node.js is too old. Install Node.js 24+ from https://nodejs.org
        echo.
        echo Installer log: %LOG_FILE%
        pause
        exit /b 1
    )
)

echo Building frontend... >> "%LOG_FILE%"
cd /d "%GITV_ROOT%\frontend"
if not exist "node_modules" (
    echo Installing frontend dependencies...
    call "!NPM_CMD!" install -q >> "%LOG_FILE%" 2>&1
) else (
    echo Updating frontend dependencies...
    call "!NPM_CMD!" install -q >> "%LOG_FILE%" 2>&1
)
echo Building frontend...
call "!NPM_CMD!" run build >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Frontend build failed. >> "%LOG_FILE%"
    cd /d "%GITV_ROOT%"
    if not exist "%GITV_ROOT%\static\index.html" (
        echo ERROR: Frontend build failed and no existing build. >> "%LOG_FILE%"
        echo Cannot start without a frontend build.
        echo.
        echo Installer log: %LOG_FILE%
        pause
        exit /b 1
    )
    echo WARNING: Build failed, using existing frontend. >> "%LOG_FILE%"
) else (
    echo Frontend built successfully. >> "%LOG_FILE%"
)
cd /d "%GITV_ROOT%"

:frontend_done
echo.

REM ============================================================
REM Configuration
REM ============================================================
echo [5/6] Checking configuration...
echo [5/6] Checking configuration... >> "%LOG_FILE%"
if not exist "%GITV_ROOT%\.env" (
    echo Creating .env from template... >> "%LOG_FILE%"
    copy "%GITV_ROOT%\.env.example" "%GITV_ROOT%\.env" >nul
)
if not exist "%GITV_ROOT%\data" mkdir "%GITV_ROOT%\data"
echo.

REM ============================================================
REM Verify installation
REM ============================================================
echo Verifying installation...
echo Verifying installation... >> "%LOG_FILE%"
set VERIFY_OK=1
if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    echo ERROR: Python venv not found >> "%LOG_FILE%"
    set VERIFY_OK=0
)
if not exist "%GITV_ROOT%\static\index.html" (
    echo ERROR: Frontend build not found >> "%LOG_FILE%"
    set VERIFY_OK=0
)
if not exist "%DENO_EXE%" (
    where deno >nul 2>&1
    if errorlevel 1 (
        echo WARNING: Deno not found >> "%LOG_FILE%"
        set VERIFY_OK=0
    )
)
if "!VERIFY_OK!"=="0" (
    echo ERROR: Installation verification failed. >> "%LOG_FILE%"
    echo ERROR: Installation verification failed. See errors above.
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)
echo All components verified. >> "%LOG_FILE%"
echo All components verified.

REM ============================================================
REM Configure Windows Firewall
REM ============================================================
echo.
echo Configuring Windows Firewall...
echo Configuring Windows Firewall... >> "%LOG_FILE%"
netsh advfirewall firewall show rule name="GitInTheVan" >nul 2>&1
if errorlevel 1 (
    netsh advfirewall firewall add rule name="GitInTheVan" dir=in action=allow protocol=TCP localport=8000 >nul 2>&1
    if errorlevel 1 (
        echo WARNING: Could not create firewall rule. >> "%LOG_FILE%"
    ) else (
        echo Firewall rule created for port 8000. >> "%LOG_FILE%"
    )
) else (
    echo Firewall rule already exists. >> "%LOG_FILE%"
)

REM ============================================================
REM Start server
REM ============================================================
echo [6/6] Starting GitInTheVan...
echo [INSTALL COMPLETE - Server starting] >> "%LOG_FILE%"
echo.
echo ============================================
echo   GitInTheVan is starting...
echo   Web UI: http://localhost:8000
echo   (or http://127.0.0.1:8000)
echo   Press Ctrl+C to stop.
echo ============================================
echo.
echo Installer log saved to: %LOG_FILE%
echo.

cd /d "%GITV_ROOT%"
"%GITV_ROOT%\.venv\Scripts\uvicorn" app.main:app --host :: --port 8000
pause
