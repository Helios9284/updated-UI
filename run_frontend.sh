#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend-react"
# Defaults chosen to not collide with the legacy dash frontend (5174) / backend (8770).
FRONTEND_PORT="${FRONTEND_PORT:-5175}"
BACKEND_PORT="${BACKEND_PORT:-8772}"
# Export so vite.config.js can point its proxy at the right backend port.
export BACKEND_PORT

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

cd "${FRONTEND_DIR}"
kill_port "${FRONTEND_PORT}"

if [[ ! -d node_modules ]]; then
  echo "Installing frontend dependencies..."
  npm install
fi

if [[ -n "${VITE_WS_URL:-}" ]]; then
  echo "Starting frontend on :${FRONTEND_PORT}, ws=${VITE_WS_URL}"
  exec env VITE_WS_URL="${VITE_WS_URL}" npm run dev -- --host 0.0.0.0 --port "${FRONTEND_PORT}" --strictPort
fi

echo "Starting frontend on :${FRONTEND_PORT}, ws=auto(hostname:${BACKEND_PORT})"
exec npm run dev -- --host 0.0.0.0 --port "${FRONTEND_PORT}" --strictPort
