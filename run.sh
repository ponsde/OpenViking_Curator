#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "[ERROR] .env not found. Copy .env.example to .env and fill your keys."
  exit 1
fi

set -a
source .env
set +a

python curator_v0.py "$@"
