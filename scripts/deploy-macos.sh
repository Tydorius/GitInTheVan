#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$(dirname "$0")/installer.log"

# Redirect all output to both console and log file
exec > >(tee "$LOG_FILE") 2>&1

echo "============================================"
echo "  GitInTheVan - macOS Deploy
echo "  Date: $(date)
echo "  Installer log: $LOG_FILE
echo "============================================"
echo

cd "$GITV_ROOT"
echo "Working directory: $GITV_ROOT"
echo

# ============================================================
# Check Python version (3.12+ required)
# ============================================================
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
            /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.14 \
            /usr/local/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3.14 \
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.14/bin/python3; do
            if [ -x "$PY_PATH" ]; then
                echo "Found Python at $PY_PATH"
                PYTHON_CMD="$PY_PATH"
                break
            fi
        done
    fi

    if [ -z "$PYTHON_CMD" ]; then
        echo "Python 3.12+ not found."
        if command -v brew &> /dev/null; then
            read -p "Would you like to install Python 3.12 via Homebrew? [y/n]: " INSTALL_PY
            if [[ "$INSTALL_PY" =~ ^[Yy]$ ]]; then
                echo "Installing Python 3.12..."
                brew install python@3.12
                hash -r 2>/dev/null
                if command -v python3.12 &> /dev/null; then
                    PYTHON_CMD="python3.12"
                elif [ -x /opt/homebrew/bin/python3.12 ]; then
                    PYTHON_CMD="/opt/homebrew/bin/python3.12"
                elif [ -x /usr/local/bin/python3.12 ]; then
                    PYTHON_CMD="/usr/local/bin/python3.12"
                else
                    echo "Installation completed but python3.12 not found."
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
fi
echo "DEBUG: PYTHON_CMD=$PYTHON_CMD"
echo

# ============================================================
# Set up Python environment
# ============================================================
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

# ============================================================
# Check Deno
# ============================================================
echo "[3/6] Checking Deno runtime..."
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    DENO_ARCH="aarch64-apple-darwin"
else
    DENO_ARCH="x86_64-apple-darwin"
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

# ============================================================
# Check Node.js and build frontend
# ============================================================
echo "[4/6] Checking Node.js and building frontend..."
NODE_CMD=""
NODE_LOCAL_DIR="$GITV_ROOT/.node"

# Check for previously downloaded local Node first
if [ -x "$NODE_LOCAL_DIR/bin/node" ]; then
    NODE_CMD="$NODE_LOCAL_DIR/bin/node"
    echo "Found local Node.js at $NODE_CMD"
fi

# Check system Node if local not found
if [ -z "$NODE_CMD" ]; then
    if command -v node &> /dev/null; then
        NODE_CMD="$(command -v node)"
        echo "Found Node.js in PATH: $NODE_CMD"
    else
        echo "Node.js not found in PATH. Searching common locations..."
        for SYS_NODE in /opt/homebrew/bin/node /usr/local/bin/node /opt/local/bin/node; do
            if [ -x "$SYS_NODE" ]; then
                NODE_CMD="$SYS_NODE"
                echo "Found Node.js at $NODE_CMD"
                break
            fi
        done
        if [ -z "$NODE_CMD" ] && [ -d "$HOME/.nvm/versions/node" ]; then
            NVM_NODE=$(ls -1d "$HOME/.nvm/versions/node/"v* 2>/dev/null | sort -V | tail -1)
            if [ -n "$NVM_NODE" ] && [ -x "$NVM_NODE/bin/node" ]; then
                NODE_CMD="$NVM_NODE/bin/node"
                echo "Found Node.js via nvm at $NODE_CMD"
            fi
        fi
        if [ -z "$NODE_CMD" ] && [ -d "$HOME/Library/Application Support/fnm/node-versions" ]; then
            FNM_NODE=$(ls -1d "$HOME/Library/Application Support/fnm/node-versions/"v*/*/bin/node 2>/dev/null | sort -V | tail -1)
            if [ -n "$FNM_NODE" ] && [ -x "$FNM_NODE" ]; then
                NODE_CMD="$FNM_NODE"
                echo "Found Node.js via fnm at $NODE_CMD"
            fi
        fi
    fi
fi

# If still no Node, offer local download
if [ -z "$NODE_CMD" ]; then
    echo "Node.js not found on system."
    if [ -f "$GITV_ROOT/static/index.html" ]; then
        echo "WARNING: Using existing frontend build."
        echo "To update the UI, install Node.js 24+ or re-run to download a local copy."
        NODE_CMD=""
    else
        echo ""
        echo "Node.js 24+ is required to build the web UI."
        echo ""
        echo "Options:"
        echo "  1. Download portable Node.js to .node/ folder (no admin/sudo required)"
        if command -v brew &> /dev/null; then
            echo "  2. Install via Homebrew: brew install node"
        fi
        echo "  3. Skip (use existing frontend if available)"
        echo ""
        read -p "Choose option [1/2/3]: " NODE_CHOICE

        if [ "$NODE_CHOICE" = "1" ]; then
            echo "Downloading portable Node.js..."
            ARCH=$(uname -m)
            if [ "$ARCH" = "arm64" ]; then
                NODE_TARBALL="node-v24.17.0-darwin-arm64.tar.gz"
            else
                NODE_TARBALL="node-v24.17.0-darwin-x64.tar.gz"
            fi
            mkdir -p "$NODE_LOCAL_DIR"
            curl -fsSL "https://nodejs.org/dist/v24.17.0/$NODE_TARBALL" -o "/tmp/gitv-node.tar.gz"
            if [ $? -ne 0 ]; then
                echo "ERROR: Failed to download Node.js."
                rm -f "/tmp/gitv-node.tar.gz"
            else
                tar -xzf "/tmp/gitv-node.tar.gz" -C "$NODE_LOCAL_DIR" --strip-components=1
                rm -f "/tmp/gitv-node.tar.gz"
                if [ -x "$NODE_LOCAL_DIR/bin/node" ]; then
                    NODE_CMD="$NODE_LOCAL_DIR/bin/node"
                    echo "Portable Node.js installed to $NODE_CMD"
                else
                    echo "ERROR: Portable Node extraction failed."
                    ls -la "$NODE_LOCAL_DIR/bin/" 2>/dev/null
                fi
            fi
        elif [ "$NODE_CHOICE" = "2" ] && command -v brew &> /dev/null; then
            echo "Installing Node.js via Homebrew..."
            brew install node
            hash -r 2>/dev/null
            if command -v node &> /dev/null; then
                NODE_CMD="$(command -v node)"
            fi
        fi
        if [ -z "$NODE_CMD" ] && [ ! -f "$GITV_ROOT/static/index.html" ]; then
            echo ""
            echo "============================================"
            echo "ERROR: Cannot start without a frontend build."
            echo "============================================"
            exit 1
        fi
    fi
fi

# Verify Node version and build
if [ -n "$NODE_CMD" ]; then
    echo "DEBUG: NODE_CMD=$NODE_CMD"
    echo "Node.js version: $($NODE_CMD --version)"

    NODE_MAJOR=$($NODE_CMD -e "process.stdout.write(process.versions.node.split('.')[0])" 2>/dev/null || echo "0")
    if [ "$NODE_MAJOR" -lt 18 ] 2>/dev/null; then
        echo "WARNING: Node.js version is too old (18+ required, 24+ recommended)."
        if [ -f "$GITV_ROOT/static/index.html" ]; then
            echo "Using existing frontend build."
        else
            echo "ERROR: Cannot build frontend with this Node.js version."
            echo "Please install Node.js 24+ from https://nodejs.org"
            exit 1
        fi
    else
        echo "Building frontend..."
        cd "$GITV_ROOT/frontend"
        if [ ! -d "node_modules" ]; then
            echo "Installing frontend dependencies..."
            "$NODE_CMD" "$(dirname "$NODE_CMD")/../lib/node_modules/npm/bin/npm-cli.js" install -q 2>/dev/null || npm install -q
        else
            echo "Updating frontend dependencies..."
            "$NODE_CMD" "$(dirname "$NODE_CMD")/../lib/node_modules/npm/bin/npm-cli.js" install -q 2>/dev/null || npm install -q
        fi
        echo "Building frontend..."
        "$NODE_CMD" "$(dirname "$NODE_CMD")/../lib/node_modules/npm/bin/npm-cli.js" run build 2>/dev/null || npm run build
        cd "$GITV_ROOT"
        echo "Frontend built successfully."
    fi
fi
echo

# ============================================================
# Configuration
# ============================================================
echo "[5/6] Checking configuration..."
if [ ! -f "$GITV_ROOT/.env" ]; then
    echo "Creating .env from template..."
    cp "$GITV_ROOT/.env.example" "$GITV_ROOT/.env"
    echo "Created .env - edit it to configure your endpoint and secret key."
fi
echo

# Create data directory
mkdir -p "$GITV_ROOT/data"

# ============================================================
# Verify installation
# ============================================================
echo "Verifying installation..."
VERIFY_OK=1
if [ ! -f "$GITV_ROOT/.venv/bin/python" ]; then
    echo "ERROR: Python venv not found at $GITV_ROOT/.venv/bin/python"
    VERIFY_OK=0
fi
if [ ! -f "$GITV_ROOT/static/index.html" ]; then
    echo "ERROR: Frontend build not found at $GITV_ROOT/static/index.html"
    echo "The web UI will not load. Ensure Node.js 24+ is installed and re-run."
    VERIFY_OK=0
fi
if [ ! -f "$DENO_BIN" ] && ! command -v deno &> /dev/null; then
    echo "WARNING: Deno not found at $DENO_BIN or in PATH"
    echo "Cantrips will not work without Deno."
    echo "Install Deno manually from https://deno.land or set GITV_DENO_PATH."
fi
if [ "$VERIFY_OK" = "0" ]; then
    echo ""
    echo "ERROR: Installation verification failed. See errors above."
    exit 1
fi
echo "All components verified."

# ============================================================
# Check macOS Firewall
# ============================================================
echo
echo "Checking macOS Firewall..."
if /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null | grep -q "enabled"; then
    PYTHON_EXE="$GITV_ROOT/.venv/bin/python"
    if /usr/libexec/ApplicationFirewall/socketfilterfw --getappblocked "$PYTHON_EXE" 2>/dev/null | grep -q "accepted"; then
        echo "macOS Firewall: Python is allowed."
    else
        echo "WARNING: macOS Application Firewall is enabled and may block incoming connections."
        echo "To allow GitInTheVan through the firewall, run:"
        echo "  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add '$PYTHON_EXE'"
        echo "  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp '$PYTHON_EXE'"
    fi
else
    echo "macOS Firewall: Not enabled (no action needed)."
fi

# ============================================================
# Start server
# ============================================================
echo "[6/6] Starting GitInTheVan..."
echo
echo "============================================"
echo "  GitInTheVan is starting..."
echo "  Web UI: http://localhost:8000"
echo "  (or http://127.0.0.1:8000)"
echo "  Press Ctrl+C to stop."
echo "============================================"
echo

cd "$GITV_ROOT"
"$GITV_ROOT/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000
