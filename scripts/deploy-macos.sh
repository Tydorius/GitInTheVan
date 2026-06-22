#!/bin/bash
set -e

echo "============================================"
echo "  GitInTheVan - macOS Deploy"
echo "============================================"
echo

cd "$(dirname "$0")/.."

# Check Python version (3.12+ required)
echo "[1/6] Checking Python..."
PYTHON_CMD=""

if command -v python3 &> /dev/null; then
    python3 --version
    if python3 -c "import sys; exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
        PYTHON_CMD="python3"
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "Python 3.12+ is required but was not found."
    if command -v brew &> /dev/null; then
        read -p "Would you like to install Python 3.12 via Homebrew? [y/n]: " INSTALL_PY
        if [[ "$INSTALL_PY" =~ ^[Yy]$ ]]; then
            echo "Installing Python 3.12..."
            brew install python@3.12
            hash -r 2>/dev/null
            if command -v python3.12 &> /dev/null; then
                PYTHON_CMD="python3.12"
            else
                echo "Installation completed but python3.12 not found in PATH."
                echo "Please open a new terminal window and re-run this script."
                exit 1
            fi
        else
            echo "Please install Python 3.12+ from https://python.org or: brew install python@3.12"
            exit 1
        fi
    else
        echo "Homebrew is not installed."
        echo "Please install Python 3.12+ from https://python.org"
        echo "Or install Homebrew first: https://brew.sh"
        exit 1
    fi
fi
echo

# Check for existing venv or create it
echo "[2/6] Setting up Python environment..."
if [ ! -f ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv .venv
fi
echo "Upgrading pip..."
.venv/bin/python -m pip install --upgrade pip -q
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
    echo "Install Node.js 24+ from https://nodejs.org and run:"
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
