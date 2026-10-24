#!/usr/bin/env bash
# Linux launcher for the Meshtastic dashboard (see start_dashboard.bat for Windows).
# Creates .venv on first run, installs requirements, then starts server.py.
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
    echo "[INFO] No $VENV yet - creating it..."
    python3 -m venv "$VENV"
fi

if ! "$PY" -c "import meshtastic" >/dev/null 2>&1; then
    echo "[INFO] Installing dependencies..."
    "$PY" -m pip install -r requirements.txt
fi

echo "[INFO] Starting dashboard server (default http://127.0.0.1:5000; see config.local.json)"
echo "[INFO] REMINDER: only one BLE client can hold the radio - close the phone app first."
exec "$PY" server.py
