#!/usr/bin/env bash
# Lead Nurture Engine - dev runner
#
# Usage:
#   ./run.sh                  start the server on 127.0.0.1:8000 with --reload
#   ./run.sh --host 0.0.0.0   bind to all interfaces (pass any uvicorn flags)
#   PORT=9000 ./run.sh        override port via env var

set -euo pipefail

# Resolve script's own directory so the script works no matter where it's invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 1. Ensure venv exists.
if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> creating virtualenv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# 2. Activate it.
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 3. Install / refresh dependencies.
#    Skip if the marker file is newer than requirements.txt to avoid reinstalling every run.
MARKER="$VENV_DIR/.deps-installed"
if [[ ! -f "$MARKER" || requirements.txt -nt "$MARKER" ]]; then
  echo "==> installing dependencies"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch "$MARKER"
fi

# 4. Stop any previously running uvicorn for this app.
if pgrep -f "uvicorn app.main:app" > /dev/null; then
  echo "==> stopping previous uvicorn process"
  pkill -f "uvicorn app.main:app" || true
  # Give the socket a moment to release.
  sleep 1
fi

# 5. Run.
echo "==> starting Lead Nurture Engine on http://$HOST:$PORT"
exec uvicorn app.main:app --host "$HOST" --port "$PORT" --reload "$@"
