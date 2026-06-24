#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-bittensor-subnet-info}"
APP_PORT="${APP_PORT:-9999}"
APP_HOST="${APP_HOST:-0.0.0.0}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PM2_BIN="${PM2_BIN:-pm2}"
HEALTH_URL="${HEALTH_URL:-http://${APP_HOST}:${APP_PORT}/health}"
RUN_GIT_PULL="${RUN_GIT_PULL:-0}"

log() {
  printf '[deploy] %s\n' "$*"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[deploy] missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

cd "$PROJECT_DIR"

require_command "$PYTHON_BIN"
require_command "$PM2_BIN"
require_command curl

if [[ ! -f "main.py" || ! -f "requirements.txt" ]]; then
  printf '[deploy] PROJECT_DIR does not look like this project: %s\n' "$PROJECT_DIR" >&2
  exit 1
fi

if [[ "$RUN_GIT_PULL" == "1" ]]; then
  require_command git
  log "updating source with git pull --ff-only"
  git pull --ff-only
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  log "creating virtual environment at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

log "installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r requirements.txt

mkdir -p logs

log "starting or restarting PM2 app: $APP_NAME"
if "$PM2_BIN" describe "$APP_NAME" >/dev/null 2>&1; then
  "$PM2_BIN" restart "$APP_NAME" --update-env
else
  "$PM2_BIN" start "$VENV_DIR/bin/python" \
    --name "$APP_NAME" \
    --cwd "$PROJECT_DIR" \
    --time \
    --output "$PROJECT_DIR/logs/pm2-out.log" \
    --error "$PROJECT_DIR/logs/pm2-error.log" \
    -- main.py
fi

"$PM2_BIN" save

log "waiting for health check: $HEALTH_URL"
for attempt in {1..30}; do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    log "deploy completed successfully"
    "$PM2_BIN" status "$APP_NAME"
    exit 0
  fi
  sleep 1
done

printf '[deploy] health check failed: %s\n' "$HEALTH_URL" >&2
"$PM2_BIN" logs "$APP_NAME" --lines 50 --nostream
exit 1
