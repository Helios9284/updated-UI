#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${ROOT_DIR}/subnet_fetch_venv/bin/python"
UPDATER="${ROOT_DIR}/validator_hotkeys_updater.py"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Missing subnet_fetch_venv python at ${VENV_PYTHON}"
  echo "Run: ./setup_subnet_fetch_venv.sh"
  exit 1
fi

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

echo "Starting validator hotkeys updater (bittensor venv)"
exec "${VENV_PYTHON}" "${UPDATER}" "$@"
