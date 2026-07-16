#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$(dirname "$0")/installer.log"

# Redirect all output to both console and log file
exec > >(tee "$LOG_FILE") 2>&1

echo "============================================"
echo "  GitInTheVan - macOS Deploy"
echo "  Date: $(date)"
echo "  Installer log: $LOG_FILE"
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
# Pinned python-build-standalone release - not "latest", see Planning/security-control-document.md.
# Bump PYBUILD_RELEASE/PYBUILD_VERSION deliberately together (they must match
# an actual published release asset).
PYBUILD_RELEASE="20260623"
PYBUILD_VERSION="3.12.13"
PYTHON_DIR="$GITV_ROOT/.python"

# Fast path: reuse an existing venv (matches update-macos.sh's approach).
# Common case on any machine that's already been set up once, and skips
# the system-wide discovery below entirely.
if [ -f "$GITV_ROOT/.venv/bin/python" ]; then
    PYTHON_CMD="$GITV_ROOT/.venv/bin/python"
    echo "Using existing virtual environment."
elif [ -f "$PYTHON_DIR/bin/python3" ]; then
    PYTHON_CMD="$PYTHON_DIR/bin/python3"
    echo "Using previously downloaded portable Python at $PYTHON_DIR."
fi

if [ -z "$PYTHON_CMD" ] && command -v python3 &> /dev/null; then
    python3 --version
    # litellm (pinned dependency) has no release supporting Python 3.14+ as of
    # this writing, so the upper bound is required, not just a floor check.
    if python3 -c "import sys; exit(0 if (3, 12) <= sys.version_info < (3, 14) else 1)" 2>/dev/null; then
        PYTHON_CMD="python3"
    elif python3 -c "import sys; exit(0 if sys.version_info >= (3, 14) else 1)" 2>/dev/null; then
        echo "Found Python 3.14+, but the litellm dependency does not yet support 3.14+."
        echo "Searching for a compatible 3.12/3.13 installation instead..."
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "Python 3.12 or 3.13 is required (3.14+ is not yet supported, see litellm). Searching for a compatible installation..."

    for PY_CAND in python3.13 python3.12; do
        if command -v "$PY_CAND" &> /dev/null; then
            echo "Found $PY_CAND"
            PYTHON_CMD="$PY_CAND"
            break
        fi
    done

    if [ -z "$PYTHON_CMD" ]; then
        for PY_PATH in \
            /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 \
            /usr/local/bin/python3.12 /usr/local/bin/python3.13 \
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3; do
            if [ -x "$PY_PATH" ]; then
                echo "Found Python at $PY_PATH"
                PYTHON_CMD="$PY_PATH"
                break
            fi
        done
    fi

    if [ -z "$PYTHON_CMD" ]; then
        echo "Python 3.12/3.13 not found on this system. Attempting portable download to .python/ (no admin required)..."
        ARCH=$(uname -m)
        if [ "$ARCH" = "arm64" ]; then
            PYBUILD_TRIPLE="aarch64-apple-darwin"
        else
            PYBUILD_TRIPLE="x86_64-apple-darwin"
        fi
        PYBUILD_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYBUILD_RELEASE}/cpython-${PYBUILD_VERSION}+${PYBUILD_RELEASE}-${PYBUILD_TRIPLE}-install_only.tar.gz"
        PYBUILD_TARBALL="$GITV_ROOT/.python_download.tar.gz"
        if curl -fsSL "$PYBUILD_URL" -o "$PYBUILD_TARBALL"; then
            rm -rf "$PYTHON_DIR"
            tar -xzf "$PYBUILD_TARBALL" -C "$GITV_ROOT"
            mv "$GITV_ROOT/python" "$PYTHON_DIR"
            rm -f "$PYBUILD_TARBALL"
            if [ -f "$PYTHON_DIR/bin/python3" ]; then
                PYTHON_CMD="$PYTHON_DIR/bin/python3"
                echo "Portable Python installed to $PYTHON_DIR"
            fi
        else
            echo "Portable Python download failed (network issue or asset unavailable)."
            rm -f "$PYBUILD_TARBALL"
        fi
    fi

    if [ -z "$PYTHON_CMD" ]; then
        echo "Python 3.12/3.13 not found."
        if command -v brew &> /dev/null; then
            # -t 20: bounded prompt, defaults to empty (declined) on timeout so a
            # script with no controlling terminal never hangs indefinitely.
            read -t 20 -p "Would you like to install Python 3.12 via Homebrew? [y/n, defaults to n in 20s]: " INSTALL_PY || true
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
                echo "Please install Python 3.12 or 3.13 from https://python.org or: brew install python@3.12"
                exit 1
            fi
        else
            echo "Homebrew is not installed."
            echo "Please install Python 3.12 or 3.13 from https://python.org"
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
# Pinned, not "latest" - see Planning/security-control-document.md. Bump deliberately.
DENO_VERSION="v2.8.3"

if [ -f "$DENO_BIN" ]; then
    echo "Deno found at $DENO_BIN"
elif command -v deno &> /dev/null; then
    echo "Deno found in PATH"
else
    echo "Deno not found. Downloading..."
    mkdir -p "$DENO_DIR"
    curl -fsSL "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-${DENO_ARCH}.zip" -o "$DENO_DIR/deno.zip"
    if [ $? -ne 0 ]; then
        echo "WARNING: Could not download Deno automatically."
        echo "Cantrips will not work. Please install Deno manually from https://deno.land"
        rm -f "$DENO_DIR/deno.zip"
    else
        # Use the venv's Python zipfile module instead of shelling out to
        # unzip, for consistency with deploy-linux.sh (some minimal Linux
        # distros don't ship unzip by default; macOS always does, but this
        # keeps both scripts on one code path).
        "$GITV_ROOT/.venv/bin/python" -c "import zipfile; zipfile.ZipFile('$DENO_DIR/deno.zip').extractall('$DENO_DIR')"
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

# Record the resolved Deno path in .env so the app reads it via Settings.
if [ -f "$DENO_BIN" ]; then
    "$GITV_ROOT/.venv/bin/python" -m app.services.env_sync --set "GITV_DENO_PATH=$DENO_BIN" >> "$LOG_FILE" 2>&1
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

# If still no Node, try a portable download automatically (no admin/sudo
# required, no interactive prompt -- see Planning/security-control-document.md
# for why this script no longer blocks on read -p), then Homebrew if
# available, then fall back to an existing frontend build if one is present.
if [ -z "$NODE_CMD" ]; then
    echo "Node.js not found on system."
    if [ -f "$GITV_ROOT/static/index.html" ]; then
        echo "WARNING: Using existing frontend build."
        echo "To update the UI, install Node.js 24+ or re-run to download a local copy."
        NODE_CMD=""
    else
        echo ""
        echo "Node.js 24+ is required to build the web UI."
        echo "Attempting automatic portable Node.js download to .node/ (no admin/sudo required)..."

        ARCH=$(uname -m)
        if [ "$ARCH" = "arm64" ]; then
            NODE_TARBALL="node-v24.17.0-darwin-arm64.tar.gz"
        else
            NODE_TARBALL="node-v24.17.0-darwin-x64.tar.gz"
        fi
        mkdir -p "$NODE_LOCAL_DIR"
        curl -fsSL "https://nodejs.org/dist/v24.17.0/$NODE_TARBALL" -o "/tmp/gitv-node.tar.gz"
        if [ $? -ne 0 ]; then
            echo "WARNING: Portable Node.js download failed."
            rm -f "/tmp/gitv-node.tar.gz"
        else
            tar -xzf "/tmp/gitv-node.tar.gz" -C "$NODE_LOCAL_DIR" --strip-components=1
            rm -f "/tmp/gitv-node.tar.gz"
            if [ -x "$NODE_LOCAL_DIR/bin/node" ]; then
                NODE_CMD="$NODE_LOCAL_DIR/bin/node"
                echo "Portable Node.js installed to $NODE_CMD"
            else
                echo "WARNING: Portable Node extraction failed."
                ls -la "$NODE_LOCAL_DIR/bin/" 2>/dev/null
            fi
        fi

        if [ -z "$NODE_CMD" ] && command -v brew &> /dev/null; then
            echo "Trying Homebrew instead..."
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
        NODE_BIN_DIR="$(dirname "$NODE_CMD")"
        NPM_CLI="$NODE_BIN_DIR/../lib/node_modules/npm/bin/npm-cli.js"
        # npm-spawned binaries (e.g. vite's `env node` shebang) need `node` to
        # be resolvable via PATH in the child process, not just the absolute
        # $NODE_CMD path used to launch npm itself - prepend the portable
        # Node's bin dir so this works even when no system Node.js is
        # installed. Do NOT fall back to a bare `npm`/`node` on failure: an
        # unrelated npm elsewhere on PATH can silently shadow the intended one.
        if [ ! -d "node_modules" ]; then
            echo "Installing frontend dependencies..."
            PATH="$NODE_BIN_DIR:$PATH" "$NODE_CMD" "$NPM_CLI" install -q
        else
            echo "Updating frontend dependencies..."
            PATH="$NODE_BIN_DIR:$PATH" "$NODE_CMD" "$NPM_CLI" install -q
        fi
        echo "Building frontend..."
        PATH="$NODE_BIN_DIR:$PATH" "$NODE_CMD" "$NPM_CLI" run build
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
echo "Syncing .env with defaults..."
"$GITV_ROOT/.venv/bin/python" -m app.services.env_sync >> "$LOG_FILE" 2>&1
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
if [ -f "$GITV_ROOT/data/ssl/cert.pem" ]; then
    if [ ! -f "$GITV_ROOT/data/ssl/ca.pem" ]; then
        echo "WARNING: cert.pem exists but ca.pem is missing, regenerating with CA chain."
        "$GITV_ROOT/.venv/bin/python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert()" >> "$LOG_FILE" 2>&1
    fi
    if [ ! -f "$GITV_ROOT/data/ssl/key.pem" ]; then
        echo "ERROR: SSL key.pem not found."
        VERIFY_OK=0
    fi
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
# Detect LAN IP (for SSL cert and startup banner)
# ============================================================
LAN_IP=$("$GITV_ROOT/.venv/bin/python" -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null || echo "")

# ============================================================
# SSL Certificate Setup (skipped if GITV_GENERATE_CERTS=false)
# ============================================================
GENERATE_CERTS=true
if grep -q "^GITV_GENERATE_CERTS=false" "$GITV_ROOT/.env" 2>/dev/null; then
    GENERATE_CERTS=false
fi

if [ "$GENERATE_CERTS" = "false" ]; then
    echo "GITV_GENERATE_CERTS=false, skipping certificate generation."
    echo "Running in HTTP mode. Use a reverse proxy for HTTPS."
else
    echo "Setting up HTTPS for LAN access..."
    if [ -f "$GITV_ROOT/data/ssl/cert.pem" ] && [ -f "$GITV_ROOT/data/ssl/ca.pem" ]; then
        echo "SSL certificate and CA already exist, skipping generation."
    else
        if [ -f "$GITV_ROOT/data/ssl/cert.pem" ]; then
            echo "cert.pem exists but ca.pem missing, regenerating with CA chain."
        fi
        echo "Generating self-signed certificate..."
        if [ -n "$LAN_IP" ]; then
            "$GITV_ROOT/.venv/bin/python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert(extra_ips=['${LAN_IP}'])" >> "$LOG_FILE" 2>&1
        else
            "$GITV_ROOT/.venv/bin/python" -c "from app.services.ssl_manager import generate_self_signed_cert; generate_self_signed_cert()" >> "$LOG_FILE" 2>&1
        fi
        if [ $? -eq 0 ]; then
            if ! grep -q "^GITV_SSL_CERTFILE=" "$GITV_ROOT/.env" 2>/dev/null; then
                echo "GITV_SSL_CERTFILE=data/ssl/cert.pem" >> "$GITV_ROOT/.env"
                echo "GITV_SSL_KEYFILE=data/ssl/key.pem" >> "$GITV_ROOT/.env"
            fi
            echo "Certificate generated. HTTPS will be active."
        else
            echo "WARNING: Certificate generation failed."
        fi
    fi
fi

# ============================================================
# Start server
# ============================================================
echo "[6/6] Starting GitInTheVan..."
echo

if [ -f "$GITV_ROOT/data/ssl/cert.pem" ]; then
    echo "============================================"
    echo "  GitInTheVan is starting with HTTPS..."
    echo "  Web UI: https://localhost:8000"
    if [ -n "$LAN_IP" ]; then echo "  LAN:    https://${LAN_IP}:8000"; fi
    echo "  Press Ctrl+C to stop."
    echo "============================================"
    echo
    echo "IMPORTANT: On each device/browser that will use this proxy:"
    if [ -n "$LAN_IP" ]; then
        echo "  1. Open https://${LAN_IP}:8000 in the browser"
        echo "  2. Accept the self-signed certificate warning"
        echo "  3. In JanitorAI, use https://${LAN_IP}:8000/v1/chat/completions"
        echo "  as the reverse proxy URL."
    else
        echo "  1. Open https://YOUR-LAN-IP:8000 in the browser"
        echo "  2. Accept the self-signed certificate warning"
        echo "  3. In JanitorAI, use https://YOUR-LAN-IP:8000/v1/chat/completions"
        echo "  as the reverse proxy URL."
    fi
    echo
else
    echo "============================================"
    echo "  GitInTheVan is starting..."
    echo "  Web UI: http://localhost:8000"
    echo "  (or http://127.0.0.1:8000)"
    echo "  Press Ctrl+C to stop."
    echo "============================================"
fi
echo

cd "$GITV_ROOT"
# Check if port 8000 is already in use
"$GITV_ROOT/.venv/bin/python" -c "import socket; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1',8000)); s.close(); exit(0 if r==0 else 1)" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "============================================"
    echo "WARNING: Port 8000 is already in use."
    echo "A GitInTheVan server may already be running."
    echo "Stop the other instance first, then re-run."
    echo "============================================"
    exit 0
fi
"$GITV_ROOT/.venv/bin/python" -m app.main
