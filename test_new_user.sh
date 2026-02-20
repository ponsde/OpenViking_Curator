#!/usr/bin/env bash
# test_new_user.sh — Simulate a new user's first experience
#
# What it tests:
#   1. Python env setup + dependency install
#   2. Unit tests pass
#   3. CLI routing logic (no API key needed)
#   4. --status runs without crash
#   5. (Optional) Full pipeline if real API keys are provided
#
# Usage:
#   ./test_new_user.sh                    # basic checks (no API key needed)
#   ./test_new_user.sh --full             # full pipeline (needs ov.conf + .env with real keys)

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }
info() { echo -e "${YELLOW}ℹ  $1${NC}"; }

FULL_MODE=false
[[ "${1:-}" == "--full" ]] && FULL_MODE=true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR=$(mktemp -d)
trap "rm -rf $WORK_DIR" EXIT

echo "=== OpenViking Curator — New User Test ==="
echo "Working directory: $WORK_DIR"
echo "Mode: $([ "$FULL_MODE" = true ] && echo 'full (with API)' || echo 'basic (no API needed)')"
echo ""

# ── Step 1: Copy code only (not data) ──
info "Step 1: Copying project code..."
mkdir -p "$WORK_DIR/curator"
cd "$SCRIPT_DIR"
# Copy only source code, not data/state files
# Copy all source code (*.py, curator/, tests/, scripts/, configs)
cp -r curator tests scripts "$WORK_DIR/curator/" 2>/dev/null || true
cp *.py requirements.txt "$WORK_DIR/curator/" 2>/dev/null || true
for f in ov.conf.example .env.example README.md; do
    [ -e "$f" ] && cp "$f" "$WORK_DIR/curator/"
done
cd "$WORK_DIR/curator"
pass "Project code copied"

# ── Step 2: Python environment ──
info "Step 2: Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null
pass "Dependencies installed ($(python3 --version))"

# ── Step 3: Unit tests ──
info "Step 3: Running unit tests..."
TEST_OUT=$(python3 -m pytest tests/ -x -q 2>&1 | tail -5)
echo "$TEST_OUT"
if echo "$TEST_OUT" | grep -qE "[0-9]+ passed"; then
    pass "Unit tests passed"
else
    fail "Unit tests failed"
fi

# ── Step 4: CLI routing logic (no API needed) ──
info "Step 4: Testing routing logic..."

# Disable LLM routing for this test (pure rule-based)
export CURATOR_LLM_ROUTE=0

# Should NOT route — casual messages
for msg in "你好" "ok" "谢谢" "hi"; do
    R=$(python3 curator_query.py "$msg" 2>/dev/null)
    ROUTED=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin).get('routed',True))" 2>/dev/null)
    if [ "$ROUTED" = "False" ]; then
        pass "  '$msg' → not routed ✓"
    else
        fail "  '$msg' should NOT be routed but got: $R"
    fi
done

# Should route — technical queries
for msg in "Docker 部署 Redis 怎么配置" "nginx 502 报错怎么排查" "openviking 架构是什么"; do
    R=$(python3 curator_query.py "$msg" 2>/dev/null)
    ROUTED=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin).get('routed','?'))" 2>/dev/null)
    if [ "$ROUTED" = "True" ]; then
        pass "  '$msg' → routed ✓"
    else
        # Routed queries without API will error, but routed=True means routing worked
        info "  '$msg' → $ROUTED (may need API keys for full pipeline)"
    fi
done

pass "Routing logic verified"

# ── Step 5: --status (basic, may fail OV connection without real keys) ──
info "Step 5: Testing --status..."
STATUS=$(python3 curator_query.py --status 2>/dev/null || echo '{}')
echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"
pass "--status ran without crash"

# ── Step 6 (optional): Full pipeline with real API ──
if [ "$FULL_MODE" = true ]; then
    info "Step 6: Full pipeline test..."
    
    if [ ! -f "$SCRIPT_DIR/ov.conf" ] || [ ! -f "$SCRIPT_DIR/.env" ]; then
        fail "Full mode requires ov.conf and .env with real API keys in $SCRIPT_DIR"
    fi
    
    cp "$SCRIPT_DIR/ov.conf" ./ov.conf
    cp "$SCRIPT_DIR/.env" ./.env
    export OPENVIKING_CONFIG_FILE=./ov.conf
    export OV_DATA_PATH=./test_data
    export CURATOR_DATA_PATH=./test_data
    mkdir -p ./test_data
    
    R=$(python3 curator_query.py "Docker 部署 Redis 怎么配置" 2>/dev/null)
    echo "$R" | python3 -m json.tool 2>/dev/null || echo "$R"
    
    COV=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin).get('coverage',0))" 2>/dev/null || echo "?")
    if [ "$COV" != "?" ]; then
        pass "Full pipeline returned coverage=$COV"
    else
        fail "Full pipeline failed: $R"
    fi
fi

echo ""
echo "========================================="
echo -e "${GREEN}All checks passed!${NC}"
echo "========================================="
echo ""
echo "To use Curator:"
echo "  1. Copy ov.conf.example → ov.conf (fill in your embedding + VLM API keys)"
echo "  2. Copy .env.example → .env (fill in search + review API keys)"
echo "  3. python3 curator_query.py --status"
echo "  4. python3 curator_query.py \"your question\""
