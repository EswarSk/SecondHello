#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/../server"
if [[ ! -f "../web/dist/index.html" && "${SECONDHELLO_SKIP_WEB_BUILD:-0}" != "1" ]]; then
  cd ../web
  pnpm install --frozen-lockfile --ignore-scripts
  pnpm exec vite build
  cd ../server
fi
if python3 -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  exec python3 asgi.py
fi
exec python3 production_server.py
