#!/bin/bash
set -e

echo "============================================"
echo "  GitInTheVan - Linux Deploy"
echo "============================================"
echo

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "Working directory: $GITV_ROOT"
echo

cd "$GITV_ROOT"

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
    echo "Python 3.12+ is required. Searching for newer Python installations..."

    for PY_CAND in python3.14 python3.13 python3.12; do
        if command -v "$PY_CAND" &> /dev/null; then
            echo "Found $PY_CAND"
            PYTHON_CMD="$PY_CAND"
            break
        fi
    done

    if [ -z "$PYTHON_CMD" ]; then
        for PY_PATH in \
            /usr/bin/python3.12 /usr/bin/python3.13 /usr/bin/python3.14 \
            /usr/local/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3.14 \
            /opt/python3.12/bin/python3 /opt/python3.13/bin/python3; do
            if [ -x "$PY_PATH" ]; then
                echo "Found Python at $PY_PATH"
                PYTHON_CMD="$PY_PATH"
                break
            fi
        done
    fi

    if [ -z "$PYTHON_CMD" ]; then
        echo "Python 3.12+ not found."

        PKG_MGR=""
        INSTALL_CMD=""
        if command -v apt-get &> /dev/null; then
            PKG_MGR="apt-get"
            INSTALL_CMD="sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv"
        elif command -v dnf &> /dev/null; then
            PKG_MGR="dnf"
            INSTALL_CMD="sudo dnf install -y python3.12"
        elif command -v pacman &> /dev/null; then
            PKG_MGR="pacman"
            INSTALL_CMD="sudo pacman -S --noconfirm python"
        fi

    if [ -n "$PKG_MGR" ]; then
        read -p "Would you like to install Python 3.12 via $PKG_MGR? [y/n]: " INSTALL_PY
        if [[ "$INSTALL_PY" =~ ^[Yy]$ ]]; then
            echo "Installing Python 3.12..."
            if ! eval "$INSTALL_CMD"; then
                echo "Installation failed. Python 3.12 may not be available in your distribution's"
                echo "default repositories. Please install it manually."
                exit 1
            fi
            hash -r 2>/dev/null
            if command -v python3.12 &> /dev/null; then
                PYTHON_CMD="python3.12"
            elif command -v python3 &> /dev/null && python3 -c "import sys; exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
                PYTHON_CMD="python3"
            else
                echo "Installation completed but Python 3.12+ not found."
                echo "Please open a new terminal and re-run this script."
                exit 1
            fi
        else
            echo "Please install Python 3.12+ via your package manager."
            exit 1
        fi
    else
        echo "Could not detect a supported package manager (apt/dnf/pacman)."
        echo "Please install Python 3.12+ manually."
        exit 1
    fi
fi
fi
echo "DEBUG: PYTHON_CMD=$PYTHON_CMD"
echo

# Check for existing venv or create it
echo "[2/6] Setting up Python environment..."
if [ ! -f "$GITV_ROOT/.venv/bin/python" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$GITV_ROOT/.venv"
fi
echo "Upgrading pip..."
"$GITV_ROOT/.venv/bin/python" -m pip install --upgrade pip -q
echo "Installing dependencies..."
"$GITV_ROOT/.venv/bin/pip" install -e "$GITV_ROOT[dev]" -q
echo "Done."
echo

# Check Deno
echo "[3/6] Checking Deno runtime..."
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    DENO_ARCH="aarch64-unknown-linux-gnu"
else
    DENO_ARCH="x86_64-unknown-linux-gnu"
fi
DENO_DIR="$GITV_ROOT/.deno"
DENO_BIN="$DENO_DIR/deno"

if [ -f "$DENO_BIN" ]; then
    echo "Deno found at $DENO_BIN"
elif command -v deno &> /dev/null; then
    echo "Deno found in PATH"
else
    echo "Deno not found. Downloading..."
    mkdir -p "$DENO_DIR"
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o "$DENO_DIR/deno.zip"
    if [ $? -ne 0 ]; then
        echo "WARNING: Could not download Deno automatically."
        echo "Cantrips will not work. Please install Deno manually from https://deno.land"
        rm -f "$DENO_DIR/deno.zip"
    else
        cd "$DENO_DIR" && unzip -o deno.zip && cd "$GITV_ROOT"
        rm -f "$DENO_DIR/deno.zip"
        chmod +x "$DENO_BIN"
        if [ -f "$DENO_BIN" ]; then
            echo "Deno installed to $DENO_BIN"
        else
            echo "WARNING: Deno download succeeded but binary not found."
            echo "Contents of $DENO_DIR:"
            ls -la "$DENO_DIR"
            echo "Please install Deno manually from https://deno.land"
        fi
    fi
fi
echo

# Check Node and build frontend
echo "[4/6] Building frontend..."
if ! command -v node &> /dev/null; then
    if [ -f "$GITV_ROOT/static/index.html" ]; then
        echo "WARNING: Node.js not found. Using existing frontend build."
        echo "To update the UI after upgrades, install Node.js 24+ from https://nodejs.org or your package manager"
    else
        echo "WARNING: Node.js not found. Frontend will not be built."
        echo "Install Node.js 24+ from https://nodejs.org or your package manager and run:"
        echo "  cd frontend && npm install && npm run build"
    fi
else
    cd "$GITV_ROOT/frontend"
    if [ ! -d "node_modules" ]; then
        echo "Installing frontend dependencies..."
        npm install -q
    else
        echo "Updating frontend dependencies..."
        npm install -q
    fi
    echo "Building frontend..."
    npm run build
    cd "$GITV_ROOT"
    echo "Frontend built."
fi
echo

# Create .env if missing
echo "[5/6] Checking configuration..."
if [ ! -f "$GITV_ROOT/.env" ]; then
    echo "Creating .env from template..."
    cp "$GITV_ROOT/.env.example" "$GITV_ROOT/.env"
    echo "Created .env - edit it to configure your endpoint and secret key."
fi
echo

# Create data directory
mkdir -p "$GITV_ROOT/data"

# Verify installation
echo "Verifying installation..."
VERIFY_OK=1
if [ ! -f "$GITV_ROOT/.venv/bin/python" ]; then
    echo "ERROR: Python venv not found at $GITV_ROOT/.venv/bin/python"
    VERIFY_OK=0
fi
if [ ! -f "$GITV_ROOT/static/index.html" ]; then
    echo "WARNING: Frontend build not found at $GITV_ROOT/static/index.html"
    echo "The web UI will not load until you build the frontend."
    VERIFY_OK=0
fi
if [ ! -f "$DENO_BIN" ] && ! command -v deno &> /dev/null; then
    echo "WARNING: Deno not found at $DENO_BIN or in PATH"
    echo "Cantrips will not work without Deno."
    VERIFY_OK=0
fi
if [ "$VERIFY_OK" = "1" ]; then
    echo "All components verified."
fi

# Start server
echo "[6/6] Starting GitInTheVan..."
echo
echo "============================================"
echo "  GitInTheVan is starting..."
echo "  Web UI: http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo "============================================"
echo

cd "$GITV_ROOT"
"$GITV_ROOT/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000
