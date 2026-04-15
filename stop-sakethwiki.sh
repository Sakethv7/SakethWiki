#!/bin/bash
lsof -t -i :8001 2>/dev/null | xargs kill -9 2>/dev/null
pkill -f 'main\.py' 2>/dev/null
pkill -f 'vite' 2>/dev/null
echo "SakethWiki stopped."
