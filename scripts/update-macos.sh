#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$GITV_ROOT/data/updater.log"
ZIP_FILE="$GITV_ROOT/data/gitinthevan.zip"

exec > >(tee "$LOG_FILE") 2>&1

echo "============================================"
echo "  GitInTheVan - macOS Auto-Update"
echo "  Date: $(date)"
echo "  Script: $(dirname "$0")"
echo "============================================"
echo

# 3-second delay to let the HTTP response return
sleep 3

cd "$GITV_ROOT"

# ============================================================
# Stop running server
# ============================================================
echo "[1/6] Stopping server if running..."
if lsof -ti:8000 > /dev/null 2>&1; then
    PID=$(lsof -ti:8000)
    echo "Server running on port 8000 (PID $PID). Stopping..."
    kill "$PID" 2>/dev/null || true
    sleep 2
    if lsof -ti:8000 > /dev/null 2>&1; then
        echo "Force killing..."
        kill -9 "$PID" 2>/dev/null || true
        sleep 1
    fi
    echo "Server stopped."
else
    echo "No server detected on port 8000."
fi
echo

# ============================================================
# Start maintenance page (served until the real server restarts)
# ============================================================
MAINT_SCRIPT="$GITV_ROOT/data/_maintenance_server.py"
if [ -f "$GITV_ROOT/.venv/bin/python" ]; then
    cat > "$MAINT_SCRIPT" << 'PYEOF'
import http.server
import socketserver

PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>GitInTheVan - Updating</title>
<style>body{font-family:sans-serif;text-align:center;padding-top:15%;background:#111;color:#eee}</style>
</head><body><h1>GitInTheVan is updating</h1>
<p>This page will refresh automatically.</p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


with ReusableTCPServer(("0.0.0.0", 8000), Handler) as httpd:
    httpd.serve_forever()
PYEOF
    nohup "$GITV_ROOT/.venv/bin/python" "$MAINT_SCRIPT" > /dev/null 2>&1 &
    disown
    echo "Maintenance page serving on port 8000 during update."
fi
echo

# ============================================================
# Backup database
# ============================================================
echo "[2/6] Backing up database..."
if [ -f "$GITV_ROOT/data/gitinthevan.db" ]; then
    BACKUP_NAME="data/gitinthevan_backup_$(date +%Y%m%d_%H%M%S).db"
    cp "$GITV_ROOT/data/gitinthevan.db" "$GITV_ROOT/$BACKUP_NAME"
    echo "Database backed up to $BACKUP_NAME"
else
    echo "No database found at data/gitinthevan.db, skipping backup."
fi
echo

# ============================================================
# Extract zip if present
# ============================================================
echo "[3/6] Extracting update zip..."

if [ -f "$ZIP_FILE" ]; then
    echo "Found $ZIP_FILE"

    EXTRACT_TEMP="$GITV_ROOT/data/_update_extract"
    rm -rf "$EXTRACT_TEMP"
    mkdir -p "$EXTRACT_TEMP"

    if command -v unzip &> /dev/null; then
        unzip -o -q "$ZIP_FILE" -d "$EXTRACT_TEMP"
    elif [ -f "$GITV_ROOT/.venv/bin/python" ]; then
        # unzip ships by default on macOS, but fall back to the venv's
        # Python zipfile module for consistency with update-linux.sh.
        "$GITV_ROOT/.venv/bin/python" -c "import zipfile; zipfile.ZipFile('$ZIP_FILE').extractall('$EXTRACT_TEMP')"
    else
        echo "ERROR: unzip command not found and no Python venv available to fall back on. Cannot extract zip."
        exit 1
    fi

    # Check if extraction produced a single top-level folder (GitHub zipball format)
    DIR_COUNT=$(find "$EXTRACT_TEMP" -maxdepth 1 -type d | tail -n +2 | wc -l)
    if [ "$DIR_COUNT" -eq 1 ] && [ -z "$(find "$EXTRACT_TEMP" -maxdepth 1 -type f)" ]; then
        TOP_DIR=$(find "$EXTRACT_TEMP" -maxdepth 1 -type d | tail -n +2 | head -1)
        echo "Found nested folder, copying contents..."
        cp -rf "$TOP_DIR"/. "$GITV_ROOT/"
    else
        echo "Copying extracted files to root..."
        cp -rf "$EXTRACT_TEMP"/. "$GITV_ROOT/"
    fi

    rm -rf "$EXTRACT_TEMP"
    rm -f "$ZIP_FILE"
    echo "Update files extracted."
else
    echo "No zip file found at $ZIP_FILE. Running reinstall only."
fi
echo

# ============================================================
# Reinstall dependencies
# ============================================================
echo "[4/6] Reinstalling Python dependencies..."
if [ -f "$GITV_ROOT/.venv/bin/python" ]; then
    "$GITV_ROOT/.venv/bin/python" -m pip install --upgrade pip -q
    "$GITV_ROOT/.venv/bin/pip" install -e "$GITV_ROOT[dev]" -q
    echo "Dependencies installed."
else
    echo "ERROR: Python venv not found. Run the full deploy script first."
    exit 1
fi
echo

# ============================================================
# Rebuild frontend
# ============================================================
echo "[5/6] Rebuilding frontend..."
NODE_CMD=""
NPM_CMD=""

if [ -x "$GITV_ROOT/.node/bin/node" ]; then
    NODE_CMD="$GITV_ROOT/.node/bin/node"
    NPM_CMD="$GITV_ROOT/.node/bin/npm"
elif command -v node &> /dev/null; then
    NODE_CMD="node"
    NPM_CMD="npm"
fi

if [ -z "$NODE_CMD" ]; then
    echo "WARNING: Node.js not found. Using existing frontend build."
else
    echo "Using Node: $NODE_CMD"
    cd "$GITV_ROOT/frontend"
    # npm's own bin/npm script (and anything it spawns, e.g. vite's
    # `env node` shebang) needs `node` resolvable via PATH, not just the
    # absolute $NODE_CMD path - required when using the portable .node/
    # install with no system-wide Node.js on PATH.
    PATH="$(dirname "$NODE_CMD"):$PATH" "$NPM_CMD" install -q
    PATH="$(dirname "$NODE_CMD"):$PATH" "$NPM_CMD" run build || echo "WARNING: Frontend build failed. Using existing build."
    cd "$GITV_ROOT"
    echo "Frontend rebuilt."
fi
echo

# ============================================================
# Start server
# ============================================================
echo "[6/6] Starting GitInTheVan..."
echo
echo "============================================"
echo "  Update complete! Starting server..."
echo "============================================"
echo

cd "$GITV_ROOT"

# Stop the maintenance page so the real server can bind port 8000
if lsof -ti:8000 > /dev/null 2>&1; then
    kill "$(lsof -ti:8000)" 2>/dev/null || true
    sleep 1
fi
rm -f "$MAINT_SCRIPT" 2>/dev/null || true

# Clean up auto-update script
rm -f "$GITV_ROOT/data/auto-update.sh" 2>/dev/null || true

"$GITV_ROOT/.venv/bin/python" -m app.main
