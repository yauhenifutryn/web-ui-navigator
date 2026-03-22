#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
RUNTIME_DIR="${ROOT_DIR}/runtime"
PORT="${PORT:-8002}"
HOST="${HOST:-127.0.0.1}"
UI_URL="http://${HOST}:${PORT}"
CHROME_APP="${CHROME_APP:-/Applications/Google Chrome.app}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-${RUNTIME_DIR}/chrome-debug-profile}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BOOTSTRAP_JSON="${RUNTIME_DIR}/bootstrap-overlay.json"

mkdir -p "${RUNTIME_DIR}" "${CHROME_PROFILE_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"

python -m pip install -U pip >/dev/null
python -m pip install -e "${ROOT_DIR}" >/dev/null

if ! pgrep -f "remote-debugging-port=9222" >/dev/null 2>&1; then
  open -na "${CHROME_APP}" --args \
    --remote-debugging-port=9222 \
    --no-first-run \
    --no-default-browser-check \
    --user-data-dir="${CHROME_PROFILE_DIR}"
fi

for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

PID_FILE="${RUNTIME_DIR}/ui_server.pid"

if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(cat "${PID_FILE}")"
  if kill -0 "${EXISTING_PID}" >/dev/null 2>&1; then
    kill "${EXISTING_PID}" 2>/dev/null || true
    sleep 1
  fi
  rm -f "${PID_FILE}"
fi

if false; then
  :
elif lsof -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${PORT} is already in use by a different process. Stop it or choose another PORT." >&2
  exit 1
else
  "${PYTHON_BIN}" - "${PID_FILE}" "${ROOT_DIR}" "${HOST}" "${PORT}" "${RUNTIME_DIR}/launch.log" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

pid_path, root_dir, host, port, log_path = sys.argv[1:]
env = os.environ.copy()
env["PYTHONPATH"] = f"{root_dir}/src"

log_handle = open(log_path, "ab", buffering=0)
process = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "marketplace_bot.api.main:app",
        "--host",
        host,
        "--port",
        port,
        "--no-access-log",
    ],
    cwd=root_dir,
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=log_handle,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
Path(sys.argv[1]).write_text(str(process.pid), encoding="utf-8")
PY
fi

for _ in $(seq 1 40); do
  if curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -fsS "${UI_URL}/api/health" >/dev/null 2>&1; then
  echo "Live Navigator local server failed to become healthy on ${UI_URL}. Check ${RUNTIME_DIR}/launch.log." >&2
  exit 1
fi

BOOTSTRAP_STATUS="$(curl -sS -o "${BOOTSTRAP_JSON}" -w '%{http_code}' -X POST "${UI_URL}/api/bootstrap-overlay" || true)"
BOOTSTRAP_OK="$(${PYTHON_BIN} - "${BOOTSTRAP_JSON}" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print('0')
else:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        print('1' if payload.get('ok', True) else '0')
    except Exception:
        print('0')
PY
)"

ENCODED_UI_URL="$(${PYTHON_BIN} - "${UI_URL}" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
)"

if [[ "${BOOTSTRAP_STATUS}" == "200" && "${BOOTSTRAP_OK}" == "1" ]]; then
  LIVE_NAVIGATOR_UI_URL="${UI_URL}" ${PYTHON_BIN} - <<'PY'
import json
import os
import urllib.request

ui_prefix = os.environ.get('LIVE_NAVIGATOR_UI_URL', '')
try:
    with urllib.request.urlopen('http://127.0.0.1:9222/json/list', timeout=2) as resp:
        tabs = json.load(resp)
except Exception:
    tabs = []
for tab in tabs:
    tab_id = tab.get('id')
    tab_url = tab.get('url', '')
    if tab_id and ui_prefix and tab_url.startswith(ui_prefix):
        try:
            urllib.request.urlopen(f'http://127.0.0.1:9222/json/close/{tab_id}', timeout=2).read()
        except Exception:
            pass
PY
  echo "Overlay bootstrapped into the active website tab. No localhost dashboard was opened."
else
  curl -fsS -X PUT "http://127.0.0.1:9222/json/new?${ENCODED_UI_URL}" >/dev/null || true
fi

cat <<EOF
Live Navigator Companion launched.

Chrome debug mode is required and is now expected on:
  http://localhost:9222

Local bootstrap / recovery page:
  ${UI_URL}

Server PID:
  $(cat "${PID_FILE}")

Server log:
  ${RUNTIME_DIR}/launch.log

Status:
  make status

Stop:
  make stop
EOF
