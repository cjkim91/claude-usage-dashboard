#!/usr/bin/env bash
# Run the dashboard locally. Reads ~/.claude and ~/.claude-personal — no API calls.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

PORT="${PORT:-8765}"
echo "→ Dashboard at http://localhost:${PORT}"
exec .venv/bin/python app.py
