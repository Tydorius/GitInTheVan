#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$GITV_ROOT/data/updater.log"
ZIP_FILE="$GITV_ROOT/data/gitinthevan.zip"

exec > >(tee "$LOG_FILE") 2>&1

echo "============================================"
echo "  GitInTheVan - Auto-Update"
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
    else
        echo "ERROR: unzip command not found. Cannot extract zip."
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
    "$NPM_CMD" install -q
    "$NPM_CMD" run build || echo "WARNING: Frontend build failed. Using existing build."
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

# Clean up auto-update script
rm -f "$GITV_ROOT/data/auto-update.sh" 2>/dev/null || true

"$GITV_ROOT/.venv/bin/python" -m app.main
