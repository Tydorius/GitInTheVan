#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$(dirname "$0")/updater.log"

exec > >(tee "$LOG_FILE") 2>&1

echo "============================================"
echo "  GitInTheVan - macOS Update"
echo "  Date: $(date)"
echo "============================================"
echo

cd "$GITV_ROOT"

# ============================================================
# Stop running server
# ============================================================
echo "[1/5] Stopping server if running..."
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
# Backup database
# ============================================================
echo "[2/5] Backing up database..."
if [ -f "$GITV_ROOT/data/gitinthevan.db" ]; then
    BACKUP_NAME="data/gitinthevan_backup_$(date +%Y%m%d_%H%M%S).db"
    cp "$GITV_ROOT/data/gitinthevan.db" "$GITV_ROOT/$BACKUP_NAME"
    echo "Database backed up to $BACKUP_NAME"
else
    echo "No database found at data/gitinthevan.db, skipping backup."
fi
echo

# ============================================================
# Reinstall dependencies
# ============================================================
echo "[3/5] Reinstalling Python dependencies..."
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
echo "[4/5] Rebuilding frontend..."
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
    "$NPM_CMD" install -q
    "$NPM_CMD" run build || echo "WARNING: Frontend build failed. Using existing build."
    cd "$GITV_ROOT"
    echo "Frontend rebuilt."
fi
echo

# ============================================================
# Start server
# ============================================================
echo "[5/5] Starting GitInTheVan..."
echo
echo "============================================"
echo "  Update complete! Starting server..."
echo "============================================"
echo

cd "$GITV_ROOT"
"$GITV_ROOT/.venv/bin/python" -m app.main
