#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/runtime"
PORT="${PORT:-8002}"
HOST="${HOST:-127.0.0.1}"
UI_URL="http://${HOST}:${PORT}"
PID_FILE="${RUNTIME_DIR}/ui_server.pid"

SERVER_PID=""
SERVER_STATUS="stopped"
HEALTH_STATUS="down"
CHROME_STATUS="down"

if [[ -f "${PID_FILE}" ]]; then
  SERVER_PID="$(cat "${PID_FILE}")"
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    SERVER_STATUS="running"
  else
    SERVER_STATUS="stale_pid_file"
  fi
fi

if curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then
  HEALTH_STATUS="healthy"
fi

if curl -fsS "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
  CHROME_STATUS="connected"
fi

cat <<EOF
Live Navigator local status
  Server process: ${SERVER_STATUS}
  Server PID: ${SERVER_PID:-unavailable}
  API health: ${HEALTH_STATUS}
  Chrome debug: ${CHROME_STATUS}
  PID file: ${PID_FILE}
  Log file: ${RUNTIME_DIR}/launch.log

Stop command:
  make stop
EOF
