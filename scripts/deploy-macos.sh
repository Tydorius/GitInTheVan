#!/bin/bash
set -e

echo "============================================"
echo "  GitInTheVan - macOS Deploy"
echo "============================================"
echo

cd "$(dirname "$0")/.."

# Check Python
echo "[1/6] Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH."
    echo "Please install Python 3.12+ from https://python.org or via Homebrew: brew install python"
    exit 1
fi
python3 --version
echo

# Check for existing venv or create it
echo "[2/6] Setting up Python environment..."
if [ ! -f ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install -e ".[dev]" -q
echo "Done."
echo

# Check Deno
echo "[3/6] Checking Deno runtime..."
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    DENO_ARCH="aarch64-apple-darwin"
else
    DENO_ARCH="x86_64-apple-darwin"
fi

if [ -f ".deno/deno" ]; then
    echo "Deno found at .deno/deno"
elif command -v deno &> /dev/null; then
    echo "Deno found in PATH"
else
    echo "Deno not found. Downloading..."
    mkdir -p .deno
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o .deno/deno.zip
    if [ $? -ne 0 ]; then
        echo "WARNING: Could not download Deno automatically."
        echo "Cantrips will not work. Please install Deno manually from https://deno.land"
        rm -f .deno/deno.zip
    else
        cd .deno && unzip -o deno.zip && cd ..
        rm -f .deno/deno.zip
        chmod +x .deno/deno
        echo "Deno installed to .deno/deno"
    fi
fi
echo

# Check Node and build frontend
echo "[4/6] Checking frontend..."
if [ -f "static/index.html" ]; then
    echo "Frontend already built. Skipping."
elif command -v node &> /dev/null; then
    echo "Building frontend..."
    cd frontend
    npm install -q
    npm run build
    cd ..
    echo "Frontend built."
else
    echo "WARNING: Node.js not found. Frontend will not be built."
    echo "Install Node.js 20+ from https://nodejs.org and run:"
    echo "  cd frontend && npm install && npm run build"
fi
echo

# Create .env if missing
echo "[5/6] Checking configuration..."
if [ ! -f ".env" ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "Created .env - edit it to configure your endpoint and secret key."
fi
echo

# Create data directory
mkdir -p data

# Start server
echo "[6/6] Starting GitInTheVan..."
echo
echo "============================================"
echo "  GitInTheVan is starting..."
echo "  Web UI: http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo "============================================"
echo

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
