#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m py_compile server/main.py server/production_server.py server/asgi.py
python3 -m unittest discover -s server -p 'test_*.py' -v
swift test

if [[ "${SECONDHELLO_SKIP_WEB_BUILD:-0}" != "1" ]]; then
  cd web
  pnpm install --frozen-lockfile --ignore-scripts
  pnpm exec vite build
fi

echo "Second Hello release checks passed."
