#!/bin/bash
# Provision one throwaway GitInTheVan container on a Docker host.
#
# Runs directly on a dock-xx machine -- no nested Docker. Exercises the
# Dockerfile (including the hash-verified requirements/docker.txt install) and
# the committed docker-compose files, which the native deploy scripts never
# touch.
#
# Arguments (all required, positional):
#   $1 run directory
#   $2 repo URL
#   $3 branch
#   $4 host port to publish
#   $5 compose file, relative to the repo root
set -u

RUN_DIR="$1"
REPO_URL="$2"
BRANCH="$3"
PORT="$4"
COMPOSE_FILE="$5"

SRC="$RUN_DIR/GitInTheVan"
LOGS="$RUN_DIR/harness-logs"
mkdir -p "$LOGS"

# Namespaced per run so concurrent runs cannot adopt each other's containers,
# and so teardown can target exactly what it created.
PROJECT="gitvtest_$(basename "$RUN_DIR" | tr -cd 'a-z0-9')"
echo "$PROJECT" > "$RUN_DIR/.compose-project"

log() { echo "[provision-docker] $*"; }
fail() { echo "[provision-docker] ERROR: $*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is not installed on this host"
command -v docker >/dev/null 2>&1 || fail "docker is not installed on this host"

if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    fail "neither 'docker compose' nor 'docker-compose' is available"
fi
log "using: $DC"

# ---------------------------------------------------------------- clone -----

log "cloning $REPO_URL branch $BRANCH"
git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$SRC" > "$LOGS/clone.log" 2>&1 \
    || { cat "$LOGS/clone.log" >&2; fail "clone failed"; }

COMMIT=$(cd "$SRC" && git rev-parse HEAD)
echo "$COMMIT" > "$RUN_DIR/.commit"
log "commit $COMMIT"

cd "$SRC" || fail "cannot enter $SRC"
[ -f "$COMPOSE_FILE" ] || fail "compose file not found: $COMPOSE_FILE"

# ---------------------------------------------------------------- build -----

# Explicit build step, separate from `up`, so a hash mismatch in
# requirements/docker.txt surfaces as a build failure with readable output
# rather than a container that silently never starts.
log "building image (this exercises --require-hashes)"
$DC -f "$COMPOSE_FILE" -p "$PROJECT" build > "$LOGS/docker-build.log" 2>&1 || {
    tail -60 "$LOGS/docker-build.log" >&2
    fail "image build failed"
}

log "starting stack"
GITV_PORT="$PORT" $DC -f "$COMPOSE_FILE" -p "$PROJECT" up -d \
    > "$LOGS/docker-up.log" 2>&1 || {
    tail -60 "$LOGS/docker-up.log" >&2
    fail "compose up failed"
}

# ------------------------------------------------------------- readiness ----

log "waiting for http://127.0.0.1:$PORT/health"
DEADLINE=$(( $(date +%s) + 600 ))
OK=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" 2>/dev/null \
        | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        OK=$((OK + 1))
        [ "$OK" -ge 2 ] && break
    else
        OK=0
    fi
    sleep 3
done

if [ "$OK" -lt 2 ]; then
    $DC -f "$COMPOSE_FILE" -p "$PROJECT" logs --tail 80 > "$LOGS/docker-logs.log" 2>&1 || true
    tail -80 "$LOGS/docker-logs.log" >&2 2>/dev/null || true
    fail "container did not become healthy within 600s"
fi

log "healthy on port $PORT"
echo "PROVISION_OK commit=$COMMIT port=$PORT project=$PROJECT"
