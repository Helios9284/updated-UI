#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
# Default 8772 so this app runs alongside the legacy dash backend (8770).
BACKEND_PORT="${BACKEND_PORT:-8772}"
BACKEND_ENDPOINT="${BACKEND_ENDPOINT:-wss://entrypoint-finney.opentensor.ai:443}"

kill_port() {
  local port="$1"
  local pids
  pids="$(ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p { if (match($0, /pid=[0-9]+/)) { print substr($0, RSTART+4, RLENGTH-4) } }' | sort -u)"
  if [[ -n "${pids}" ]]; then
    echo "Killing existing process(es) on :${port} -> ${pids}"
    # shellcheck disable=SC2086
    kill ${pids} || true
    sleep 0.3
  fi
}

cd "${ROOT_DIR}"
kill_port "${BACKEND_PORT}"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing venv python at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Load .env so wallet passwords / chain endpoint are present in the environment
# before the stake service is constructed at import time.
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

echo "Starting combined server: ${BACKEND_HOST}:${BACKEND_PORT} endpoint=${BACKEND_ENDPOINT} (stake API at /stake-api)"
exec "${PYTHON_BIN}" "${ROOT_DIR}/server.py" --host "${BACKEND_HOST}" --port "${BACKEND_PORT}" --endpoint "${BACKEND_ENDPOINT}"
