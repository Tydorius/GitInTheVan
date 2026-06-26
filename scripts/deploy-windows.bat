@echo off
setlocal enabledelayedexpansion
title GitInTheVan Deploy

echo ============================================
echo   GitInTheVan - Windows Deploy
echo ============================================
echo.

echo Working directory: %CD%
echo Script location: %~dp0
echo.

cd /d "%~dp0\.."
set "GITV_ROOT=%CD%"
echo Changed to: %GITV_ROOT%
echo.

REM Check Python version (3.12+ required)
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

REM python found but too old — search for a newer version
echo.
echo Python !PYMAJOR!.!PYMINOR! found but 3.12+ required.
echo Searching for newer Python installations...

REM Try py launcher first
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

REM Search user APPDATA (per-user installs)
for %%P in (312 313 314) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe" (
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python%%P\python.exe"
        echo Found Python at !PYTHON_CMD!
        goto :python_done
    )
)

REM Search Program Files (system-wide installs)
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

REM No newer version found — offer to install
goto :python_offer_install

:python_not_found
REM No python at all in PATH
echo Python not found in PATH.

:python_offer_install
echo.
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.12+ is required but winget is not available.
    echo Please install Python 3.12+ from https://python.org
    pause
    exit /b 1
)

echo Python 3.12+ is required.
set "INSTALL_PY="
set /p INSTALL_PY="Would you like to install Python 3.12 via winget? [y/n]: "
if /i not "!INSTALL_PY!"=="y" (
    echo Please install Python 3.12+ from https://python.org
    pause
    exit /b 1
)

echo.
echo Installing Python 3.12 via winget...
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo ERROR: winget installation failed.
    echo Please install Python 3.12+ manually from https://python.org
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

echo Python installed but not found in expected locations.
echo Please close this window, open a new command prompt, and re-run this script.
pause
exit /b 1

:python_found_default
set "PYTHON_CMD=python"
echo DEBUG: Using default python from PATH

:python_done
echo DEBUG: PYTHON_CMD=[!PYTHON_CMD!]
if "!PYTHON_CMD!"=="" (
    echo ERROR: PYTHON_CMD is empty. Python was not detected properly.
    echo Please report this error with the debug output above.
    pause
    exit /b 1
)
echo.

REM Check for existing venv or create it
echo [2/6] Setting up Python environment...
if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    echo Creating virtual environment...
    echo DEBUG: Running command: "!PYTHON_CMD!" -m venv "%GITV_ROOT%\.venv"
    "!PYTHON_CMD!" -m venv "%GITV_ROOT%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        echo DEBUG: PYTHON_CMD was [!PYTHON_CMD!]
        echo DEBUG: errorlevel was !errorlevel!
        pause
        exit /b 1
    )
) else (
    echo DEBUG: .venv already exists, skipping creation
)
echo Upgrading pip...
"%GITV_ROOT%\.venv\Scripts\python" -m pip install --upgrade pip -q
echo Installing dependencies...
"%GITV_ROOT%\.venv\Scripts\pip" install -e "%GITV_ROOT%.[dev]" -q
if errorlevel 1 (
    echo ERROR: Failed to install Python dependencies.
    echo This usually means pip is outdated or Python version is incompatible.
    pause
    exit /b 1
)
echo Done.
echo.

REM Check Deno
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
                echo Cantrips will not work. Please install Deno manually from https://deno.land
                echo Or download and place at %DENO_EXE%
            )
        )
    )
)
echo.

REM Check Node and build frontend
echo [4/6] Building frontend...
where node >nul 2>&1
if errorlevel 1 (
    if exist "%GITV_ROOT%\static\index.html" (
        echo WARNING: Node.js not found. Using existing frontend build.
        echo To update the UI after upgrades, install Node.js 24+ from https://nodejs.org
    ) else (
        echo WARNING: Node.js not found. Frontend will not be built.
        echo Install Node.js 24+ from https://nodejs.org and run:
        echo   cd frontend ^&^& npm install ^&^& npm run build
    )
) else (
    cd /d "%GITV_ROOT%\frontend"
    if not exist "node_modules" (
        echo Installing frontend dependencies...
        call npm install -q
    ) else (
        echo Updating frontend dependencies...
        call npm install -q
    )
    echo Building frontend...
    call npm run build
    cd /d "%GITV_ROOT%"
    echo Frontend built.
)
echo.

REM Create .env if missing
echo [5/6] Checking configuration...
if not exist "%GITV_ROOT%\.env" (
    echo Creating .env from template...
    copy "%GITV_ROOT%\.env.example" "%GITV_ROOT%\.env" >nul
    echo Created .env - edit it to configure your endpoint and secret key.
)
echo.

REM Create data directory
if not exist "%GITV_ROOT%\data" mkdir "%GITV_ROOT%\data"

REM Verify installation
echo Verifying installation...
set VERIFY_OK=1
if not exist "%GITV_ROOT%\.venv\Scripts\python.exe" (
    echo ERROR: Python venv not found at %GITV_ROOT%\.venv\Scripts\python.exe
    set VERIFY_OK=0
)
if not exist "%GITV_ROOT%\static\index.html" (
    echo WARNING: Frontend build not found at %GITV_ROOT%\static\index.html
    echo The web UI will not load until you build the frontend.
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
if "!VERIFY_OK!"=="1" echo All components verified.

REM Start server
echo [6/6] Starting GitInTheVan...
echo.
echo ============================================
echo   GitInTheVan is starting...
echo   Web UI: http://localhost:8000
echo   Press Ctrl+C to stop.
echo ============================================
echo.

cd /d "%GITV_ROOT%"
"%GITV_ROOT%\.venv\Scripts\uvicorn" app.main:app --host 0.0.0.0 --port 8000
pause
