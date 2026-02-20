# Contributing to OpenViking Curator

## Architecture

Curator is OV's **governance layer** — it does NOT replace OV's core capabilities.

### What Curator does
- Coverage assessment (trust OV's score)
- External search when local knowledge is insufficient
- AI review + ingest back to OV
- Conflict detection (local vs external)
- Freshness scoring (semantic layer, OV补充)
- Resource dedup scanning (report-only)

### What Curator does NOT do (OV handles these)
- Retrieval / semantic search
- L0/L1/L2 content loading
- Memory extraction (session commit)
- Memory deduplication
- Answer generation

## Code Structure

```
curator/
  config.py          # Env vars, thresholds
  router.py          # Lightweight rule-based routing
  retrieval_v2.py    # On-demand L0→L1→L2 + coverage assessment
  session_manager.py # OV HTTP client + session lifecycle
  search.py          # External search + cross-validation
  review.py          # AI review, ingest, conflict detection
  freshness.py       # Time-decay freshness scoring
  dedup.py           # Resource dedup scanning (report-only)
  pipeline_v2.py     # Main 5-step pipeline
  legacy/            # Archived v1 modules
```

## Development

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in API keys

# Run tests
python -m pytest tests/ -v

# Run benchmark
python eval/benchmark.py
```

## Guidelines

1. **Trust OV's score** — don't re-rank or re-score retrieval results
2. **Minimize LLM calls** — merge prompts when possible, skip when unnecessary
3. **Strict on-demand** — L0 first, L1 only if needed, L2 only if L1 insufficient
4. **Return data, not answers** — pipeline returns structured context for the caller
5. **All thresholds in config** — never hardcode magic numbers

## Pull Request Process

1. Run `pytest tests/` — all tests must pass
2. Update README if the change affects public API
3. Add tests for new functionality
4. Keep commits atomic and messages clear
