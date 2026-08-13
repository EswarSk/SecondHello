#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")/../server"
exec python3 main.py
