#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p data

START_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START_EPOCH="$(date +%s)"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

PYTEST_CMD="pytest tests/ -q"
if [ -x ./.venv/bin/pytest ]; then
  PYTEST_CMD="./.venv/bin/pytest tests/ -q"
fi

SMOKE_CMD="bash scripts/smoke_minimal.sh"

echo "[regression] run pytest"
set +e
bash -lc "$PYTEST_CMD"
PYTEST_CODE=$?
set -e

if [ "$PYTEST_CODE" -eq 0 ]; then
  PYTEST_STATUS="passed"
else
  PYTEST_STATUS="failed"
fi

echo "[regression] run smoke"
set +e
bash -lc "$SMOKE_CMD"
SMOKE_CODE=$?
set -e

if [ "$SMOKE_CODE" -eq 0 ]; then
  SMOKE_STATUS="passed"
else
  SMOKE_STATUS="failed"
fi

END_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
END_EPOCH="$(date +%s)"
TOTAL_SEC=$((END_EPOCH - START_EPOCH))

if [ "$PYTEST_CODE" -eq 0 ] && [ "$SMOKE_CODE" -eq 0 ]; then
  OVERALL_STATUS="passed"
  EXIT_CODE=0
else
  OVERALL_STATUS="failed"
  EXIT_CODE=1
fi

cat > data/regression-last.json <<JSON
{
  "started_at": "$START_TS",
  "finished_at": "$END_TS",
  "git_commit": "$COMMIT",
  "total_duration_sec": $TOTAL_SEC,
  "pytest": {
    "command": "$PYTEST_CMD",
    "status": "$PYTEST_STATUS",
    "exit_code": $PYTEST_CODE
  },
  "smoke_minimal": {
    "command": "$SMOKE_CMD",
    "status": "$SMOKE_STATUS",
    "exit_code": $SMOKE_CODE
  },
  "overall_status": "$OVERALL_STATUS"
}
JSON

echo "[regression] wrote data/regression-last.json"
exit "$EXIT_CODE"
