#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/runtime"
PORT="${PORT:-8002}"
HOST="${HOST:-127.0.0.1}"
UI_URL="http://${HOST}:${PORT}"
PID_FILE="${RUNTIME_DIR}/ui_server.pid"

if curl -fsS -X POST "${UI_URL}/api/stop" >/dev/null 2>&1; then
  echo "Stopped the active overlay session."
else
  echo "Overlay session stop request was skipped because the local API is not responding."
fi

if [[ ! -f "${PID_FILE}" ]]; then
  echo "No PID file found. The local server is already stopped or was not launched with scripts/launch_local.sh."
  exit 0
fi

PID="$(cat "${PID_FILE}")"
if kill -0 "${PID}" >/dev/null 2>&1; then
  kill "${PID}"
  for _ in $(seq 1 20); do
    if ! kill -0 "${PID}" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
fi

rm -f "${PID_FILE}"
echo "Live Navigator local server stopped."
