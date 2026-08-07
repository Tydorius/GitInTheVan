#!/bin/bash
set -e

GITV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$GITV_ROOT/data/updater.log"
ZIP_FILE="$GITV_ROOT/data/gitinthevan.zip"
CHAIN_LOG="$GITV_ROOT/data/update-chain.log"

# Arguments are optional and order-independent so that a NEW app version can
# drive an OLD copy of this script (which happens on the first hop of every
# upgrade) without the extra arguments breaking anything.
#   --auto        unattended; never wait for a keypress
#   <port>        port the server listens on (default 8000)
GITV_PORT=8000
GITV_AUTO=0
for arg in "$@"; do
    case "$arg" in
        --auto) GITV_AUTO=1 ;;
        [0-9]*) GITV_PORT="$arg" ;;
    esac
done

# Kill whatever is LISTENING on the port given as $1.
#
# Only ever a fallback, for when the PID files below are missing or stale. lsof
# is absent on many minimal distros, so ss and fuser are tried too rather than
# letting one missing tool be fatal: the Windows script shipped a bare `netstat`
# as its single point of failure and stranded 0.18.0 installs permanently when
# it could not be resolved.
#
# Every PID, not PID=$(...): a server listening on both IPv4 and IPv6 produces
# two lines, and `kill "$PID"` on "123\n124" fails outright, killing neither.
kill_port_holders() {
    port="$1"
    pids=""
    if command -v lsof > /dev/null 2>&1; then
        pids=$(lsof -ti:"$port" 2>/dev/null || true)
    elif command -v ss > /dev/null 2>&1; then
        pids=$(ss -ltnp 2>/dev/null | grep -E "[:.]$port[[:space:]]" \
            | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)
    elif command -v fuser > /dev/null 2>&1; then
        pids=$(fuser -n tcp "$port" 2>/dev/null || true)
    fi
    [ -n "$pids" ] || return 0
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 2
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    return 0
}

# Rotate the previous run's log instead of truncating it. A chained upgrade runs
# this script once per hop, and without rotation each hop destroys the evidence
# needed to diagnose the one before it.
mkdir -p "$GITV_ROOT/data/update-logs"
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "$GITV_ROOT/data/update-logs/updater-$(date +%Y%m%d_%H%M%S).log" 2>/dev/null || true
    ls -1t "$GITV_ROOT"/data/update-logs/updater-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f
fi

exec > >(tee "$LOG_FILE") 2>&1

# `set -e` otherwise aborts silently mid-update -- after the new files are
# extracted but before any server is started -- leaving the maintenance page
# serving "updating" forever, indistinguishable from progress.
trap 'echo "UPDATE FAILED at line $LINENO (exit $?)" | tee -a "$CHAIN_LOG"' ERR

echo "============================================"
echo "  GitInTheVan - macOS Auto-Update"
echo "  Date: $(date)"
echo "  Script: $(dirname "$0")"
echo "  Port: $GITV_PORT"
echo "============================================"
echo

# 3-second delay to let the HTTP response return
sleep 3

cd "$GITV_ROOT"

# ============================================================
# Stop running server
# ============================================================
echo "[1/6] Stopping server if running..."
# app/main.py writes data/gitv.pid at startup, so prefer it over a port scan.
# It can outlive the process -- a -9 skips the atexit cleanup that would have
# removed it -- so the port is swept afterwards rather than trusting the PID to
# have been the real holder.
if [ -f "$GITV_ROOT/data/gitv.pid" ]; then
    SERVER_PID=$(cat "$GITV_ROOT/data/gitv.pid" 2>/dev/null || true)
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Server running on port $GITV_PORT (PID $SERVER_PID). Stopping..."
        kill "$SERVER_PID" 2>/dev/null || true
        sleep 2
        if kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "Force killing..."
            kill -9 "$SERVER_PID" 2>/dev/null || true
            sleep 1
        fi
    fi
    rm -f "$GITV_ROOT/data/gitv.pid" 2>/dev/null || true
fi
# Anything still on the port never wrote a PID file (a manual uvicorn run, say).
kill_port_holders "$GITV_PORT"
echo "Server stopped."
echo

# ============================================================
# Start maintenance page (served until the real server restarts)
# ============================================================
MAINT_SCRIPT="$GITV_ROOT/data/_maintenance_server.py"
if [ -f "$GITV_ROOT/.venv/bin/python" ]; then
    cat > "$MAINT_SCRIPT" << 'PYEOF'
import http.server
import os
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


with ReusableTCPServer(("0.0.0.0", int(os.environ.get("GITV_MAINT_PORT", "8000"))), Handler) as httpd:
    httpd.serve_forever()
PYEOF
    GITV_MAINT_PORT="$GITV_PORT" nohup "$GITV_ROOT/.venv/bin/python" "$MAINT_SCRIPT" > /dev/null 2>&1 &
    # Record the PID so the teardown below does not have to find it by scanning.
    echo $! > "$GITV_ROOT/data/_maintenance.pid"
    disown
    echo "Maintenance page serving on port $GITV_PORT during update."
fi
echo

# ============================================================
# Backup database
# ============================================================
# Per hop, not once per chain: each hop's migrations are what can corrupt, so a
# per-hop rollback point is the right granularity.
echo "[2/6] Backing up database..."
if [ -f "$GITV_ROOT/data/gitinthevan.db" ]; then
    BACKUP_NAME="data/gitinthevan_backup_$(date +%Y%m%d_%H%M%S).db"
    cp "$GITV_ROOT/data/gitinthevan.db" "$GITV_ROOT/$BACKUP_NAME"
    echo "Database backed up to $BACKUP_NAME"
    # Prune to the newest 10. maxdepth 1 only: data/backups/ uses the same
    # filename prefix for scheduled backups, and those are managed elsewhere.
    ls -1t "$GITV_ROOT"/data/gitinthevan_backup_*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
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
    # Pinned like every other dependency (exact pins only, never ranges) - an unpinned
    # `--upgrade pip` is an unreviewed network fetch on every single update.
    "$GITV_ROOT/.venv/bin/python" -m pip install "pip==26.2" -q
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
    # `npm ci` installs strictly from package-lock.json. `npm install` would
    # re-resolve against the live registry and rewrite the lockfile, which
    # defeats the exact pinning required by the dependency pinning policy.
    #
    # Guarded with `if`: under `set -e` an unguarded failure would abort the
    # update after the new files are already extracted but before the server is
    # restarted. Falling back to the existing static/ build is recoverable; a
    # half-finished update is not. Never fall back to `npm install`.
    if PATH="$(dirname "$NODE_CMD"):$PATH" "$NPM_CMD" ci -q; then
        PATH="$(dirname "$NODE_CMD"):$PATH" "$NPM_CMD" run build || echo "WARNING: Frontend build failed. Using existing build."
    else
        echo "WARNING: npm ci failed (package.json/package-lock.json may disagree). Using existing frontend build."
    fi
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

# Stop the maintenance page so the real server can bind the port.
#
# The most load-bearing step in this script. The maintenance page holds the port
# for the whole update and nothing else in the product ever releases it, so if
# this fails the install can never serve again -- not on this run and not on any
# later one. Kill by recorded PID first and treat scanning as the fallback;
# 0.18.0 had scanning alone, and on Windows a missing netstat made that fatal.
if [ -f "$GITV_ROOT/data/_maintenance.pid" ]; then
    MAINT_PID=$(cat "$GITV_ROOT/data/_maintenance.pid" 2>/dev/null || true)
    if [ -n "$MAINT_PID" ]; then
        kill "$MAINT_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$MAINT_PID" 2>/dev/null; then
            kill -9 "$MAINT_PID" 2>/dev/null || true
        fi
        echo "Stopped maintenance page PID $MAINT_PID"
    fi
    rm -f "$GITV_ROOT/data/_maintenance.pid" 2>/dev/null || true
fi
kill_port_holders "$GITV_PORT"
rm -f "$MAINT_SCRIPT" 2>/dev/null || true

# Clean up auto-update script.
#
# Do NOT remove data/update-chain.json here. It carries the frozen multi-release
# upgrade plan across restarts, and the newly started server reads it to decide
# whether another hop is due. data/ is gitignored and absent from the release
# zip, which is exactly why chain state lives there and survives extraction.
rm -f "$GITV_ROOT/data/auto-update.sh" 2>/dev/null || true

# Redirect rather than inheriting the tee above: this call blocks for the
# server's entire lifetime, so without this every line the server ever prints
# lands in updater.log. Python logging is already captured by
# setup_file_logging() in data/logs/gitinthevan.log.
mkdir -p "$GITV_ROOT/data/logs"
"$GITV_ROOT/.venv/bin/python" -m app.main >> "$GITV_ROOT/data/logs/server-stdout.log" 2>&1
