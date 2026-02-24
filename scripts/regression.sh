#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[1/3] Unit tests"
if [ -x ./.venv/bin/pytest ]; then
  ./.venv/bin/pytest tests/ -q
else
  pytest tests/ -q
fi

echo "[2/3] Smoke test"
bash scripts/smoke_minimal.sh

echo "[3/3] Benchmark"
if python3 - <<'PY'
import socket
s=socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1",9100))
    ok=True
except Exception:
    ok=False
finally:
    s.close()
raise SystemExit(0 if ok else 1)
PY
then
  python3 eval/benchmark.py
else
  echo "SKIP benchmark: OV HTTP (127.0.0.1:9100) not reachable"
fi

echo "DONE"
