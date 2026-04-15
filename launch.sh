#!/bin/bash
# Kill any existing instances
lsof -t -i :8001 2>/dev/null | xargs kill -9 2>/dev/null
pkill -f 'main\.py' 2>/dev/null
pkill -f 'vite' 2>/dev/null
sleep 1

PROJECT="/Users/sakethv7/Sakethwiki"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Start backend (use venv python directly to avoid arch mismatch)
nohup /bin/bash -c "cd '$PROJECT/backend' && exec '$PROJECT/backend/venv/bin/python3' main.py" > /tmp/sakethwiki-backend.log 2>&1 &

# Start frontend
nohup /bin/bash -c "export PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin' && cd '$PROJECT/frontend' && npm run dev" > /tmp/sakethwiki-frontend.log 2>&1 &

# Wait for Vite to be ready then open browser
sleep 5
open "http://localhost:5173"
