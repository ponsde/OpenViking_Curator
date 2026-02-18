# Contributing to OpenViking Curator

Thanks for your interest! Here's how to get started.

## Quick Setup

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator

# Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Config
cp .env.example .env
# Edit .env with your API keys

# Run tests
python -m pytest tests/ -v
```

## Project Structure

```
curator.py          # Core pipeline (8-step: route → search → judge → answer)
curator_query.py       # CLI entry point with routing gate
search_providers.py    # Pluggable external search backends
mcp_server.py          # MCP server (stdio JSON-RPC)
feedback_store.py      # Feedback storage (thread-safe)
memory_capture.py      # Auto case capture after queries
freshness_rescan.py    # URL liveness + TTL expiry scanner
dedup.py               # AI-powered dedup (scan/clean/merge)
batch_ingest.py        # Bulk topic ingestion
eval_batch.py          # Benchmark evaluation (10 questions)
metrics.py             # Runtime metrics collection
maintenance.py         # Feedback decay + expired case check
tests/test_core.py     # Unit tests (22 tests)
```

## Adding a Search Provider

1. Edit `search_providers.py`
2. Create a function: `def my_provider(query, scope, **kwargs) -> str`
3. Register it: `PROVIDERS["my_provider"] = my_provider`
4. Set env: `CURATOR_SEARCH_PROVIDER=my_provider`

Example:
```python
def bing_search(query: str, scope: dict, **kwargs) -> str:
    # Your Bing API logic here
    ...
    return search_result_text

PROVIDERS["bing"] = bing_search
```

## Running Tests

```bash
python -m pytest tests/ -v
```

All tests should pass without external API calls (they test internal logic only).

## Code Style

- Python 3.10+
- Keep functions focused and testable
- Add docstrings to public functions
- New features should include tests in `tests/test_core.py`

## Submitting Changes

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Commit with descriptive messages
4. Push and open a PR

## Reporting Issues

- Include the error output
- Include your Python version and OS
- For search/retrieval issues, include the query and expected behavior

## License

MIT — see [LICENSE](LICENSE).
