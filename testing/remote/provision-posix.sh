#!/bin/bash
# Provision one throwaway GitInTheVan install on a macOS or Linux target.
#
# Invoked by testing/harness.py over SSH. Deliberately calls the repo's real
# deploy script rather than reimplementing it -- the end-user install path is
# what is under test, so anything that diverges here tests the wrong thing.
#
# Arguments (all required, positional):
#   $1 run directory   e.g. ~/github/_gitv-testruns/20260818-101500-ab12
#   $2 repo URL
#   $3 branch
#   $4 port
#   $5 mock upstream port (0 disables)
set -u

RUN_DIR="$1"
REPO_URL="$2"
BRANCH="$3"
PORT="$4"
MOCK_PORT="$5"

SRC="$RUN_DIR/GitInTheVan"
LOGS="$RUN_DIR/harness-logs"
mkdir -p "$LOGS"

log() { echo "[provision] $*"; }
fail() { echo "[provision] ERROR: $*" >&2; exit 1; }

case "$(uname -s)" in
    Darwin) DEPLOY="deploy-macos.sh" ;;
    Linux)  DEPLOY="deploy-linux.sh" ;;
    *)      fail "unsupported platform: $(uname -s)" ;;
esac
log "platform $(uname -s), using scripts/$DEPLOY"

# ---------------------------------------------------------------- clone -----

command -v git >/dev/null 2>&1 || fail "git is not installed on this host"

log "cloning $REPO_URL branch $BRANCH"
git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$SRC" > "$LOGS/clone.log" 2>&1 \
    || { cat "$LOGS/clone.log" >&2; fail "clone failed"; }

COMMIT=$(cd "$SRC" && git rev-parse HEAD)
echo "$COMMIT" > "$RUN_DIR/.commit"
log "commit $COMMIT"

# ----------------------------------------------------------------- port -----

# The deploy scripts hardcode 8000 in their banner but honour GITV_PORT from
# .env. Write it before deploying so the server binds where we expect.
mkdir -p "$SRC/data"
if [ -f "$SRC/.env.example" ]; then
    cp "$SRC/.env.example" "$SRC/.env"
else
    : > "$SRC/.env"
fi
{
    echo "GITV_PORT=$PORT"
    echo "GITV_HTTP_REDIRECT_PORT=0"
    echo "GITV_LOG_LEVEL=INFO"
} >> "$SRC/.env"

# ---------------------------------------------------------- mock upstream ---

if [ "$MOCK_PORT" != "0" ]; then
    PY=$(command -v python3 || command -v python) \
        || fail "no python on PATH to run the mock upstream"
    log "starting mock upstream on port $MOCK_PORT"
    nohup "$PY" "$RUN_DIR/mock_upstream.py" --port "$MOCK_PORT" \
        > "$LOGS/mock-upstream.log" 2>&1 &
    echo $! > "$RUN_DIR/.mock.pid"
fi

# --------------------------------------------------------------- deploy -----

# The deploy script ends in a foreground `python -m app.main`, so it is
# backgrounded and readiness is decided by polling /health. Its exit code is
# not trustworthy on its own: it exits 0 when the port is already in use.
log "running scripts/$DEPLOY (backgrounded; readiness polled separately)"
chmod +x "$SRC/scripts/$DEPLOY" 2>/dev/null || true
cd "$SRC" || fail "cannot enter $SRC"
nohup bash "scripts/$DEPLOY" > "$LOGS/deploy.log" 2>&1 &
echo $! > "$RUN_DIR/.deploy.pid"

# ------------------------------------------------------------- readiness ----

log "waiting for http://127.0.0.1:$PORT/health"
PY=$(command -v python3 || command -v python) || fail "no python on PATH"
DEADLINE=$(( $(date +%s) + 900 ))
OK=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if "$PY" - "$PORT" <<'EOF' >/dev/null 2>&1
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
    sys.exit(0 if json.load(r).get("status") == "ok" else 1)
EOF
    then
        OK=$((OK + 1))
        # Twice, because the update system's maintenance page also binds this
        # port and serves HTML for every path -- a listening socket proves
        # nothing. Same reasoning as app/services/updater.py.
        [ "$OK" -ge 2 ] && break
    else
        OK=0
    fi
    sleep 3
done

if [ "$OK" -lt 2 ]; then
    echo "--- deploy.log (tail) ---" >&2
    tail -60 "$LOGS/deploy.log" >&2 2>/dev/null || true
    fail "server did not become healthy within 900s"
fi

# Record the server PID so teardown can stop it without a port scan.
"$PY" - "$SRC" > "$RUN_DIR/.server.pid" 2>/dev/null <<'EOF' || true
import pathlib, sys
pid = pathlib.Path(sys.argv[1], "data", "gitv.pid")
print(pid.read_text().strip() if pid.exists() else "")
EOF

log "healthy on port $PORT"
echo "PROVISION_OK commit=$COMMIT port=$PORT"
