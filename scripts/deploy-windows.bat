@echo off
setlocal enabledelayedexpansion
title GitInTheVan Deploy

echo ============================================
echo   GitInTheVan - Windows Deploy
echo ============================================
echo.

cd /d "%~dp0\.."

REM Check Python
echo [1/6] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python 3.12+ from https://python.org
    pause
    exit /b 1
)
python --version
echo.

REM Check for existing venv or create it
echo [2/6] Setting up Python environment...
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)
echo Installing dependencies...
".venv\Scripts\pip" install -e ".[dev]" -q
echo Done.
echo.

REM Check Deno
echo [3/6] Checking Deno runtime...
if exist ".deno\deno.exe" (
    echo Deno found at .deno\deno.exe
) else (
    where deno >nul 2>&1
    if not errorlevel 1 (
        echo Deno found in PATH
    ) else (
        echo Deno not found. Downloading...
        if not exist ".deno" mkdir ".deno"
        powershell -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile '.deno\deno.zip'"
        if errorlevel 1 (
            echo WARNING: Could not download Deno automatically.
            echo Cantrips will not work. Please install Deno manually from https://deno.land
            echo Or download and place at .deno\deno.exe
        ) else (
            powershell -Command "Expand-Archive -Path '.deno\deno.zip' -DestinationPath '.deno\' -Force"
            del ".deno\deno.zip"
            echo Deno installed to .deno\deno.exe
        )
    )
)
echo.

REM Check Node and build frontend
echo [4/6] Checking frontend...
if exist "static\index.html" (
    echo Frontend already built. Skipping.
) else (
    where node >nul 2>&1
    if errorlevel 1 (
        echo WARNING: Node.js not found. Frontend will not be built.
        echo Install Node.js 20+ from https://nodejs.org and run:
        echo   cd frontend ^&^& npm install ^&^& npm run build
    ) else (
        echo Building frontend...
        cd frontend
        call npm install -q
        call npm run build
        cd ..
        echo Frontend built.
    )
)
echo.

REM Create .env if missing
echo [5/6] Checking configuration...
if not exist ".env" (
    echo Creating .env from template...
    copy ".env.example" ".env" >nul
    echo Created .env - edit it to configure your endpoint and secret key.
)
echo.

REM Create data directory
if not exist "data" mkdir "data"

REM Start server
echo [6/6] Starting GitInTheVan...
echo.
echo ============================================
echo   GitInTheVan is starting...
echo   Web UI: http://localhost:8000
echo   Press Ctrl+C to stop.
echo ============================================
echo.

".venv\Scripts\uvicorn" app.main:app --host 0.0.0.0 --port 8000
pause
