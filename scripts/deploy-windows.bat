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
REM Detect PowerShell (may not be on PATH on some systems)
REM ============================================================
set "PS_CMD="
where powershell >nul 2>&1
if not errorlevel 1 (
    set "PS_CMD=powershell"
) else if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
    set "PS_CMD=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    echo DEBUG: PowerShell not on PATH, using system location >> "%LOG_FILE%"
) else (
    echo WARNING: PowerShell not found. File downloads may fail. >> "%LOG_FILE%"
)
echo DEBUG: PS_CMD=[!PS_CMD!] >> "%LOG_FILE%"

REM ============================================================
REM Check Python version (3.12 or 3.13 required; 3.14+ unsupported by litellm)
REM ============================================================
echo [1/6] Checking Python...
echo [1/6] Checking Python... >> "%LOG_FILE%"
set "PYTHON_CMD="
REM Pinned python-build-standalone release - not "latest", see
REM Planning/security-control-document.md. Bump PYBUILD_RELEASE/PYBUILD_VERSION
REM deliberately together (they must match an actual published release asset).
set "PYBUILD_RELEASE=20260623"
set "PYBUILD_VERSION=3.12.13"
set "PYTHON_DIR=%GITV_ROOT%\.python"

REM Fast path: reuse an existing venv (matches update-windows.bat's approach).
REM This is the common case on any machine that's already been set up once,
REM and skips the fragile system-wide discovery below entirely.
if exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%GITV_ROOT%\.venv\Scripts\python.exe"
    echo Using existing virtual environment. >> "%LOG_FILE%"
    goto :python_done
)
if exist "%PYTHON_DIR%\python.exe" (
    set "PYTHON_CMD=%PYTHON_DIR%\python.exe"
    echo Using previously downloaded portable Python at %PYTHON_DIR%. >> "%LOG_FILE%"
    goto :python_done
)

python --version >nul 2>&1
if errorlevel 1 goto :python_search

python --version >> "%LOG_FILE%" 2>&1
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
echo DEBUG: PYVER=!PYVER! PYMAJOR=!PYMAJOR! PYMINOR=!PYMINOR! >> "%LOG_FILE%"

REM litellm (pinned dependency) has no release supporting Python 3.14+ as of
REM this writing, so an upper bound is required, not just a floor check.
if !PYMAJOR! EQU 3 if !PYMINOR! GEQ 12 if !PYMINOR! LSS 14 (
    set "PYTHON_CMD=python"
    goto :python_done
)

echo.
if !PYMAJOR! EQU 3 if !PYMINOR! GEQ 14 (
    echo Python !PYMAJOR!.!PYMINOR! found, but the litellm dependency does not yet support 3.14+.
    echo Python !PYMAJOR!.!PYMINOR! found, but litellm does not yet support 3.14+. >> "%LOG_FILE%"
) else (
    echo Python !PYMAJOR!.!PYMINOR! found but 3.12 or 3.13 required.
    echo Python !PYMAJOR!.!PYMINOR! found but 3.12 or 3.13 required. >> "%LOG_FILE%"
)

:python_search
echo Searching for a compatible Python installation...
echo Searching for a compatible Python installation... >> "%LOG_FILE%"

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

for %%P in (312 313) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe" (
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe"
        echo Found Python at !PYTHON_CMD! >> "%LOG_FILE%"
        goto :python_done
    )
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

echo.
echo Python 3.12/3.13 not found. Attempting portable download to .python\ (no admin required)...
echo Python 3.12/3.13 not found. Attempting portable download to .python\... >> "%LOG_FILE%"
where tar >nul 2>&1
if not errorlevel 1 (
    set "PYBUILD_URL=https://github.com/astral-sh/python-build-standalone/releases/download/%PYBUILD_RELEASE%/cpython-%PYBUILD_VERSION%+%PYBUILD_RELEASE%-x86_64-pc-windows-msvc-install_only.tar.gz"
    set "PYBUILD_TARBALL=%GITV_ROOT%\.python_download.tar.gz"
    powershell -Command "$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri '!PYBUILD_URL!' -OutFile '!PYBUILD_TARBALL!'" >> "%LOG_FILE%" 2>&1
    if exist "!PYBUILD_TARBALL!" (
        if exist "%PYTHON_DIR%" rmdir /s /q "%PYTHON_DIR%"
        tar -xzf "!PYBUILD_TARBALL!" -C "%GITV_ROOT%" >> "%LOG_FILE%" 2>&1
        move /y "%GITV_ROOT%\python" "%PYTHON_DIR%" >nul 2>&1
        del "!PYBUILD_TARBALL!" >nul 2>&1
        if exist "%PYTHON_DIR%\python.exe" (
            set "PYTHON_CMD=%PYTHON_DIR%\python.exe"
            echo Portable Python installed to %PYTHON_DIR% >> "%LOG_FILE%"
            goto :python_done
        )
    )
    echo Portable Python download failed - network issue or asset unavailable. >> "%LOG_FILE%"
) else (
    echo tar.exe not available - requires Windows 10 1803+. Skipping portable download. >> "%LOG_FILE%"
)

echo.
echo Python 3.12 or 3.13 was not found on this system (3.14+ is not yet supported, see litellm). >> "%LOG_FILE%"
echo Python 3.12 or 3.13 was not found on this system (3.14+ is not yet supported, see litellm).
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: winget is not available either. >> "%LOG_FILE%"
    echo Please install Python 3.12 or 3.13 from https://python.org and re-run this script.
    echo.
    echo Installer log: %LOG_FILE%
    pause
    exit /b 1
)

echo.
REM Bounded prompt (20s, defaults to No) instead of a blocking set /p - a
REM script with no console attached (e.g. run non-interactively) must not
REM hang here indefinitely. See Planning/security-control-document.md.
choice /c YN /n /t 20 /d N /m "Install Python 3.12 via winget now? [Y/N, defaults to N in 20s]"
if errorlevel 2 (
    echo Python install declined or prompt timed out. >> "%LOG_FILE%"
    echo Please install Python 3.12 or 3.13 from https://python.org and re-run this script.
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
    echo Please install Python 3.12 or 3.13 manually from https://python.org
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
REM Pinned, not "latest" - see Planning/security-control-document.md. Bump deliberately.
set "DENO_VERSION=v2.8.3"

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
        "!PS_CMD!" -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/download/%DENO_VERSION%/deno-x86_64-pc-windows-msvc.zip' -OutFile '!DENO_ZIP!'" >> "%LOG_FILE%" 2>&1
        if errorlevel 1 (
            echo WARNING: Could not download Deno. >> "%LOG_FILE%"
        ) else (
            "!PS_CMD!" -Command "Expand-Archive -Path '!DENO_ZIP!' -DestinationPath '%DENO_DIR%' -Force" >> "%LOG_FILE%" 2>&1
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

REM Record the resolved Deno path in .env so the app reads it via Settings.
if exist "%DENO_EXE%" (
    "%GITV_ROOT%\.venv\Scripts\python.exe" -m app.services.env_sync --set "GITV_DENO_PATH=%DENO_EXE%" >> "%LOG_FILE%" 2>&1
)
echo.

REM ============================================================
REM Check Node.js and build frontend
REM ============================================================
echo [4/6] Checking Node.js and building frontend...
echo [4/6] Checking Node.js and building frontend... >> "%LOG_FILE%"
set "NODE_CMD="
set "NPM_CMD="
set "NODE_LOCAL_DIR=%GITV_ROOT%\.node"

REM Check for previously downloaded local Node first
if exist "%NODE_LOCAL_DIR%\node.exe" (
    set "NODE_CMD=%NODE_LOCAL_DIR%\node.exe"
    set "NPM_CMD=%NODE_LOCAL_DIR%\npm.cmd"
    echo Found local Node.js at !NODE_CMD! >> "%LOG_FILE%"
    goto :node_verify
)

REM Check PATH
where node >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
    where npm >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
    )
    echo Found Node.js in PATH: !NODE_CMD! >> "%LOG_FILE%"
    goto :node_verify
)

REM Check common system locations
if exist "C:\Program Files\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
    echo Found Node.js in Program Files >> "%LOG_FILE%"
    goto :node_verify
)
if exist "C:\Program Files (x86)\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files (x86)\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files (x86)\nodejs\npm.cmd"
    echo Found Node.js in Program Files ^(x86^) >> "%LOG_FILE%"
    goto :node_verify
)
if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" (
    set "NODE_CMD=%LOCALAPPDATA%\Programs\nodejs\node.exe"
    set "NPM_CMD=%LOCALAPPDATA%\Programs\nodejs\npm.cmd"
    echo Found Node.js in LOCALAPPDATA >> "%LOG_FILE%"
    goto :node_verify
)

REM Check nvm-windows
for %%v in (24 23 22 21 20) do (
    for /f "delims=" %%p in ('dir /b /ad "%LOCALAPPDATA%\nvm\v%%v*" 2^>nul') do (
        if exist "%LOCALAPPDATA%\nvm\%%p\node.exe" (
            set "NODE_CMD=%LOCALAPPDATA%\nvm\%%p\node.exe"
            set "NPM_CMD=%LOCALAPPDATA%\nvm\%%p\npm.cmd"
            echo Found Node.js via nvm-windows >> "%LOG_FILE%"
            goto :node_verify
        )
    )
)

REM Check fnm
if exist "%LOCALAPPDATA%\fnm_multishells" (
    for /f "delims=" %%p in ('dir /b /ad /s "%LOCALAPPDATA%\fnm_multishells\*\node.exe" 2^>nul') do (
        set "NODE_CMD=%%p"
        echo Found Node.js via fnm >> "%LOG_FILE%"
        goto :node_verify
    )
)

REM No system Node found. Try a portable download automatically (no admin
REM required, no interactive prompt - see Planning/security-control-document.md
REM for why this script no longer blocks on set /p), then winget, then fall
REM back to an existing frontend build if one is present.
echo Node.js not found on system. >> "%LOG_FILE%"
echo.
echo Node.js 24+ is required to build the web UI.
echo Attempting automatic portable Node.js download to .node\ ^(no admin required^)...

:node_download_local
echo.
echo Downloading portable Node.js...
echo Downloading portable Node.js... >> "%LOG_FILE%"
if not exist "%NODE_LOCAL_DIR%" mkdir "%NODE_LOCAL_DIR%"
set "NODE_ZIP=%NODE_LOCAL_DIR%\node.zip"
"!PS_CMD!" -Command "$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri 'https://nodejs.org/dist/v24.17.0/node-v24.17.0-win-x64.zip' -OutFile '%NODE_ZIP%'" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Portable Node.js download failed, trying winget instead. >> "%LOG_FILE%"
    del "%NODE_ZIP%" 2>nul
    goto :node_install_winget
)
echo Extracting...
"!PS_CMD!" -Command "$ProgressPreference = 'SilentlyContinue'; Expand-Archive -Path '%NODE_ZIP%' -DestinationPath '%NODE_LOCAL_DIR%' -Force" >> "%LOG_FILE%" 2>&1
del "%NODE_ZIP%"
REM The zip extracts to a subfolder like node-v24.17.0-win-x64\
REM Move the contents up one level
for /d %%d in ("%NODE_LOCAL_DIR%\node-*") do (
    move /y "%%d\*" "%NODE_LOCAL_DIR%\" >nul 2>&1
    move /y "%%d\node_modules" "%NODE_LOCAL_DIR%\" >nul 2>&1
    for /d %%s in ("%%d\*") do (
        move /y "%%s" "%NODE_LOCAL_DIR%\" >nul 2>&1
    )
    rd "%%d" 2>nul
)
if exist "%NODE_LOCAL_DIR%\node.exe" (
    set "NODE_CMD=%NODE_LOCAL_DIR%\node.exe"
    set "NPM_CMD=%NODE_LOCAL_DIR%\npm.cmd"
    echo Portable Node.js installed to !NODE_CMD! >> "%LOG_FILE%"
    echo Portable Node.js installed.
    goto :node_verify
) else (
    echo WARNING: Portable Node extraction failed, trying winget instead. >> "%LOG_FILE%"
    echo Contents of !NODE_LOCAL_DIR!: >> "%LOG_FILE%"
    dir "%NODE_LOCAL_DIR%" /b >> "%LOG_FILE%" 2>&1
    goto :node_install_winget
)

:node_install_winget
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: winget not available. >> "%LOG_FILE%"
    goto :node_check_existing
)
echo Installing Node.js LTS via winget...
echo Installing Node.js via winget... >> "%LOG_FILE%"
winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo ERROR: winget Node.js install failed. >> "%LOG_FILE%"
    goto :node_check_existing
)
where node >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%n in ('where node') do set "NODE_CMD=%%n"
    where npm >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%n in ('where npm') do set "NPM_CMD=%%n"
    )
    goto :node_verify
)
if exist "C:\Program Files\nodejs\node.exe" (
    set "NODE_CMD=C:\Program Files\nodejs\node.exe"
    set "NPM_CMD=C:\Program Files\nodejs\npm.cmd"
    goto :node_verify
)
echo Node.js installed but not found on PATH. >> "%LOG_FILE%"
goto :node_check_existing

:node_check_existing
if exist "%GITV_ROOT%\static\index.html" (
    echo WARNING: Using existing frontend build. >> "%LOG_FILE%"
    echo WARNING: Using existing frontend build.
    goto :frontend_done
)
echo.
echo ============================================
echo ERROR: Cannot start without a frontend build.
echo Node.js is required.
echo ============================================
echo ERROR: No Node.js and no existing frontend build. >> "%LOG_FILE%"
echo.
echo Installer log: %LOG_FILE%
pause
exit /b 1

:node_verify
echo DEBUG: NODE_CMD=[!NODE_CMD!] >> "%LOG_FILE%"
echo Node.js version: >> "%LOG_FILE%"
"!NODE_CMD!" --version >> "%LOG_FILE%" 2>&1

REM Extract major version number and compare numerically (18+ required)
set "NODE_VER_RAW="
for /f "delims=" %%v in ('"!NODE_CMD!" --version 2^>nul') do set "NODE_VER_RAW=%%v"
echo DEBUG: NODE_VER_RAW=[!NODE_VER_RAW!] >> "%LOG_FILE%"

REM Strip the 'v' prefix: v24.17.0 -> 24.17.0
set "NODE_VER_CLEAN=!NODE_VER_RAW:~1!"
for /f "tokens=1 delims=." %%m in ("!NODE_VER_CLEAN!") do set "NODE_MAJOR=%%m"
echo DEBUG: NODE_MAJOR=[!NODE_MAJOR!] >> "%LOG_FILE%"

REM Numerically compare (if NODE_MAJOR is a number)
set "NODE_OK=0"
if !NODE_MAJOR! GEQ 18 set "NODE_OK=1"

if "!NODE_OK!"=="0" (
    echo WARNING: Node.js version too old ^(found !NODE_VER_RAW!, need v18+^). >> "%LOG_FILE%"
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
echo Syncing .env with defaults... >> "%LOG_FILE%"
"!GITV_ROOT!\.venv\Scripts\python" -m app.services.env_sync >> "%LOG_FILE%" 2>&1
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
        echo WARNING: Deno not found. Cantrips will not work. >> "%LOG_FILE%"
        echo WARNING: Deno not found. Cantrips will not work.
        echo   Install Deno manually from https://deno.land or set GITV_DENO_PATH
    )
)
if exist "%GITV_ROOT%\data\ssl\cert.pem" (
    if not exist "%GITV_ROOT%\data\ssl\ca.pem" (
        echo WARNING: cert.pem exists but ca.pem is missing. >> "%LOG_FILE%"
        echo   Regenerating certificates with CA chain...
        "!GITV_ROOT!\.venv\Scripts\python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert()" >> "%LOG_FILE%" 2>&1
    )
    if not exist "%GITV_ROOT%\data\ssl\key.pem" (
        echo ERROR: SSL key.pem not found. >> "%LOG_FILE%"
        set VERIFY_OK=0
    )
) else (
    if exist "%GITV_ROOT%\.env" (
        findstr /b "GITV_SSL_CERTFILE=" "%GITV_ROOT%\.env" >nul 2>&1
        if not errorlevel 1 (
            echo WARNING: SSL configured in .env but cert.pem not found. >> "%LOG_FILE%"
        )
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
netsh advfirewall firewall add rule name="GitInTheVan HTTP Redirect" dir=in action=allow protocol=TCP localport=80 >nul 2>&1

REM ============================================================
REM SSL Certificate Setup (skipped if GITV_GENERATE_CERTS=false)
REM ============================================================
set "GENERATE_CERTS=true"
findstr /b "GITV_GENERATE_CERTS=false" "%GITV_ROOT%\.env" >nul 2>&1
if not errorlevel 1 set "GENERATE_CERTS=false"

if "!GENERATE_CERTS!"=="false" (
    echo GITV_GENERATE_CERTS=false, skipping certificate generation. >> "%LOG_FILE%"
    echo Running in HTTP mode. Use a reverse proxy for HTTPS.
    goto :ssl_done
)

echo Setting up HTTPS for LAN access...
echo Setting up HTTPS for LAN access... >> "%LOG_FILE%"
"!GITV_ROOT!\.venv\Scripts\python" -c "import socket, os; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); ip=s.getsockname()[0]; s.close(); open(os.path.join(os.environ.get('GITV_ROOT','.'),'data','ssl','lan_ip.txt'),'w').write(ip)" >nul 2>&1
set "LAN_IP="
if exist "%GITV_ROOT%\data\ssl\lan_ip.txt" (
    set /p LAN_IP=<"%GITV_ROOT%\data\ssl\lan_ip.txt"
    del "%GITV_ROOT%\data\ssl\lan_ip.txt" >nul 2>&1
)
echo DEBUG: LAN_IP=[!LAN_IP!] >> "%LOG_FILE%"

if exist "%GITV_ROOT%\data\ssl\cert.pem" (
    if exist "%GITV_ROOT%\data\ssl\ca.pem" (
        echo SSL certificate and CA already exist, skipping generation. >> "%LOG_FILE%"
        goto :ssl_done
    )
    echo cert.pem exists but ca.pem missing, regenerating with CA chain. >> "%LOG_FILE%"
)

echo Generating self-signed certificate...
echo Generating SSL certificate... >> "%LOG_FILE%"
if "!LAN_IP!"=="" (
    "!GITV_ROOT!\.venv\Scripts\python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert()" >> "%LOG_FILE%" 2>&1
) else (
    echo Detected LAN IP: !LAN_IP! >> "%LOG_FILE%"
    "!GITV_ROOT!\.venv\Scripts\python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert(extra_ips=['!LAN_IP!'])" >> "%LOG_FILE%" 2>&1
)
if errorlevel 1 (
    echo WARNING: Certificate generation failed. >> "%LOG_FILE%"
    goto :ssl_done
)
findstr /b "GITV_SSL_CERTFILE=" "%GITV_ROOT%\.env" >nul 2>&1
if errorlevel 1 (
    echo GITV_SSL_CERTFILE=data/ssl/cert.pem>> "%GITV_ROOT%\.env"
    echo GITV_SSL_KEYFILE=data/ssl/key.pem>> "%GITV_ROOT%\.env"
)
echo Certificate generated. HTTPS will be active. >> "%LOG_FILE%"

:ssl_done
REM ============================================================
REM Start server
REM ============================================================
echo [6/6] Starting GitInTheVan...
echo [INSTALL COMPLETE - Server starting] >> "%LOG_FILE%"
echo.

if not exist "%GITV_ROOT%\data\ssl\cert.pem" goto :start_http

echo ============================================
echo   GitInTheVan is starting with HTTPS...
echo   Web UI: https://localhost:8000
if not "!LAN_IP!"=="" echo   LAN:    https://!LAN_IP!:8000
echo   Press Ctrl+C to stop.
echo ============================================
echo.
echo IMPORTANT: On each device/browser that will use this proxy:
if not "!LAN_IP!"=="" goto :show_lan_instructions
echo   1. Open https://YOUR-LAN-IP:8000 in the browser
echo   2. Click "Advanced" then "Accept the Risk" ^(self-signed cert^)
echo   3. In JanitorAI, use https://YOUR-LAN-IP:8000/v1/chat/completions
echo      as the reverse proxy URL.
goto :start_server

:show_lan_instructions
echo   1. Open https://!LAN_IP!:8000 in the browser
echo   2. Click "Advanced" then "Accept the Risk" ^(self-signed cert^)
echo   3. In JanitorAI, use https://!LAN_IP!:8000/v1/chat/completions
echo      as the reverse proxy URL.
goto :start_server

:start_http
echo ============================================
echo   GitInTheVan is starting...
echo   Web UI: http://localhost:8000
echo   (or http://127.0.0.1:8000)
echo   Press Ctrl+C to stop.
echo ============================================

:start_server
echo.
echo Installer log saved to: %LOG_FILE%
echo.

cd /d "%GITV_ROOT%"
REM Check if port 8000 is already in use (another instance running)
"!GITV_ROOT!\.venv\Scripts\python" -c "import socket; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1',8000)); s.close(); exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
    echo ============================================
    echo WARNING: Port 8000 is already in use.
    echo A GitInTheVan server may already be running.
    echo Close the other instance first, then re-run.
    echo ============================================
    echo.
    pause
    exit /b 0
)
"%GITV_ROOT%\.venv\Scripts\python" -m app.main
pause
