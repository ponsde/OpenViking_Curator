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
  config.py            # Env vars, thresholds, HTTP client
  settings.py          # Pydantic Settings v2 (typed, validated)
  logging_setup.py     # structlog bridge (JSON / console toggle)
  router.py            # Lightweight rule-based routing (JSON-configurable)
  retrieval_v2.py      # On-demand L0→L1→L2 + coverage assessment
  session_manager.py   # OV HTTP client + session lifecycle
  search.py            # External search + cross-validation
  search_providers.py  # Pluggable provider chain (grok/duckduckgo/tavily)
  search_cache.py      # Query-level search cache with TTL
  review.py            # AI review, ingest, conflict detection
  review_cli.py        # Pending review CLI (approve/reject/list)
  freshness.py         # Time-decay freshness scoring
  dedup.py             # Two-layer dedup (URL hash + Jaccard)
  pipeline_v2.py       # Main pipeline orchestrator
  circuit_breaker.py   # Per-model circuit breaker
  domain_filter.py     # Whitelist/blacklist for search results
  usage_ttl.py         # Usage-based TTL tier management
  async_jobs.py        # Async ingest job tracking + recovery
  decision_report.py   # Human-readable pipeline decision summary
  memory_capture.py    # Case/pattern capture templates
  file_lock.py         # Advisory file locking for concurrent writes
  feedback_store.py    # Per-URI up/down/adopt signal store
  backend.py           # KnowledgeBackend ABC
  backend_ov.py        # OpenViking backend (HTTP + embedded)
  backend_memory.py    # InMemory backend (tests)
  metrics.py           # Pipeline metrics collection
  legacy/              # Archived v1 modules
```

## Development

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # Fill in API keys

# Run all tests (430+)
python -m pytest tests/ -v

# Run single test file
python -m pytest tests/test_core.py -v

# Run single test by name
python -m pytest tests/test_core.py -k "test_route_scope" -v

# Health check
python3 curator_query.py --status

# Run benchmark
python eval/benchmark.py
```

## Testing Patterns

All tests use `InMemoryBackend` — no OV dependency, no network required.

```python
from curator.backend_memory import InMemoryBackend

def test_something(monkeypatch):
    backend = InMemoryBackend()
    backend.ingest("content", title="test", metadata={})
    monkeypatch.setattr("curator.config.chat", lambda *a, **kw: mock_response)
    result = run("query", backend=backend)
```

Key conventions:
- All LLM calls are monkeypatched (`curator.config.chat`)
- Config overrides via `monkeypatch.setattr("curator.config.X", value)`
- No network calls in any test
- Tests are fast and deterministic

## Commit Style

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>

Types: feat, fix, refactor, docs, test, chore, perf, ci
```

Examples:
- `feat: add circuit breaker for LLM calls`
- `fix: coverage clamp to [0, 1] range`
- `test: add 8 settings validation tests`

## Guidelines

1. **Trust OV's score** — don't re-rank or re-score retrieval results
2. **Minimize LLM calls** — merge prompts when possible, skip when unnecessary
3. **Strict on-demand** — L0 first, L1 only if needed, L2 only if L1 insufficient
4. **Return data, not answers** — pipeline returns structured context for the caller
5. **All thresholds in config** — never hardcode magic numbers
6. **Backend agnostic** — never import `openviking` directly in pipeline/review code

## Pull Request Process

1. Run `pytest tests/` — all 430+ tests must pass
2. Update README if the change affects public API
3. Add tests for new functionality (aim for 80%+ coverage)
4. Keep commits atomic and messages clear
