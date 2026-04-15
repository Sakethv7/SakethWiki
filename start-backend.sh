#!/bin/bash
# Kill any existing backend on port 8001 before starting
existing=$(lsof -ti :8001 2>/dev/null)
if [ -n "$existing" ]; then
  echo "Killing existing backend (PID $existing)…"
  kill $existing 2>/dev/null
  sleep 1
fi
cd "$(dirname "$0")/backend"
source venv/bin/activate
python3 main.py
