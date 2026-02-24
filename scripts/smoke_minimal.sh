#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=python3
[ -x ./.venv/bin/python ] && PY=./.venv/bin/python

echo "smoke: --help"
$PY curator_query.py --help >/dev/null 2>&1

echo "smoke: routing (rule mode)"
export CURATOR_LLM_ROUTE=0

check_contains() {
  local msg="$1"
  local needle="$2"
  local out
  out="$($PY curator_query.py "$msg" 2>&1 || true)"
  if ! printf '%s' "$out" | grep -q "$needle"; then
    echo "smoke failed: '$msg' missing pattern: $needle"
    echo "---- output ----"
    echo "$out"
    exit 1
  fi
}

check_contains "你好" '"routed": false'
check_contains "ok" '"routed": false'
check_contains "Docker 部署 Redis 怎么配置" '"routed": true'

echo "smoke: --status"
$PY curator_query.py --status >/dev/null 2>&1 || true

echo "smoke ok"
