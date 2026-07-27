#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/subnet_fetch_venv"

cd "${ROOT_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install -U pip
"${VENV_DIR}/bin/pip" install "bittensor>=10.0"

echo "Subnet fetch venv ready: ${VENV_DIR}"
echo "Test:"
"${VENV_DIR}/bin/python" "${ROOT_DIR}/subnet_fetch_helper.py" "${SUBNET_FETCH_ENDPOINT:-ws://127.0.0.1:9944}" | tail -1
