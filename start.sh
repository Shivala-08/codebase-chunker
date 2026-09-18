#!/usr/bin/env bash
# CodeGraph one-command launcher.
# Serves app + API at http://localhost:8000 — keep this terminal window open.
set -euo pipefail
cd "$(dirname "$0")/backend"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating venv + installing deps…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "⚠️  backend/.env is missing — run: cp .env.example .env  (then add your NVIDIA_API_KEY)"
fi

echo "Starting CodeGraph → http://localhost:8000  (Ctrl+C to stop)"
exec .venv/bin/python -m uvicorn main:app --reload --port 8000
