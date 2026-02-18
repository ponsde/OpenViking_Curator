# OpenViking Curator

English / [中文](README_CN.md)

**Active knowledge governance for [OpenViking](https://github.com/volcengine/OpenViking).** Not just retrieve — decide, verify, and grow.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)
[![Tests: 22 passing](https://img.shields.io/badge/Tests-22%20passing-brightgreen.svg)](tests/)

## What is this?

Traditional RAG: put data in → get data out. Curator adds the governance layer:

| Feature | What it does |
|---------|-------------|
| **Coverage Gate** | Measures local retrieval quality; only triggers external search when needed |
| **Pluggable External Search** | Grok, OpenAI, or your own backend — just set an env var |
| **AI Review + Ingest** | Reviews external results for quality/freshness before storing |
| **Cross-Validation** | Verifies volatile claims against official sources |
| **Conflict Detection** | Identifies contradictions between local and external sources |
| **Freshness Tracking** | TTL metadata, expiry scanning, stale knowledge detection |
| **Feedback Loop** | User feedback influences future retrieval ranking |
| **Case Capture** | Automatically saves Q&A as reusable experience |

## Quick Start

### Option A: Local install

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Edit with your API keys
```

### Option B: Docker

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp .env.example .env   # Edit with your API keys
docker compose build
docker compose run curator "What is OpenViking?"
```

### First run

```bash
# Health check
python3 curator_query.py --status

# Ask a question
python3 curator_query.py "Redis vs Memcached for high concurrency?"
```

**Output (JSON):**
```json
{"routed": true, "answer": "...", "meta": {"coverage": 0.7, "external_triggered": false}}
```

## Architecture

```
Query → Gate (rule-based, no LLM) → Scope Router → Local Search (OpenViking)
                                                         ↓
                                               Coverage + Core Keywords
                                                    ↙         ↘
                                          Sufficient?     External Search (pluggable)
                                              ↓                    ↓
                                           Answer         Cross-Validate → Review → Ingest?
                                              ↓                                    ↓
                                        Source Footer                          Answer
                                              ↓                                    ↓
                                        Case Capture                     Conflict Detection
```

**8-step pipeline:** Init → Route → Local Search → External Search → Cross-Validate → Review/Ingest → Conflict Detection → Answer

## Search Providers

External search is **pluggable**. Set `CURATOR_SEARCH_PROVIDER` in `.env`:

| Provider | Value | Description |
|----------|-------|-------------|
| Grok | `grok` (default) | Via grok2api or compatible endpoint |
| OpenAI | `oai` | Any OAI-compatible model with internet access |
| Custom | your name | Register in `search_providers.py` |

**Adding your own provider:**

```python
# In search_providers.py
def my_search(query: str, scope: dict, **kwargs) -> str:
    # Your search logic (Bing, SerpAPI, internal wiki, etc.)
    return result_text

PROVIDERS["my_search"] = my_search
```

Then set `CURATOR_SEARCH_PROVIDER=my_search`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Configuration

All config via environment variables (`.env` file, git-ignored):

| Variable | Required | Description |
|----------|----------|-------------|
| `CURATOR_OAI_BASE` | ✅ | OpenAI-compatible API base URL |
| `CURATOR_OAI_KEY` | ✅ | API key for router/judge/answer |
| `CURATOR_GROK_KEY` | ✅* | Grok API key (*only if using Grok provider) |
| `CURATOR_SEARCH_PROVIDER` | | Search backend: `grok` (default), `oai`, custom |
| `CURATOR_ROUTER_MODELS` | | Model fallback chain for routing |
| `CURATOR_ANSWER_MODELS` | | Model fallback chain for answers |
| `CURATOR_JUDGE_MODELS` | | Model fallback chain for review |
| `OPENVIKING_CONFIG_FILE` | | Path to OpenViking ov.conf |

### Tunable Thresholds

All coverage/quality thresholds are configurable via env:

| Variable | Default | Meaning |
|----------|---------|---------|
| `CURATOR_THRESHOLD_LOW_COV` | 0.45 | Below → trigger external search |
| `CURATOR_THRESHOLD_CORE_COV` | 0.4 | Core keyword coverage; ≤ → external search |
| `CURATOR_THRESHOLD_LOW_TRUST` | 5.4 | Below → quality boost search |
| `CURATOR_THRESHOLD_LOW_FRESH` | 0.25 | Below → freshness boost search |

**No secrets are hardcoded.** All sensitive values come from `.env`.

## Repo Structure

```
curator.py          # Core 8-step pipeline
curator_query.py       # CLI entry (--help, --status, query)
search_providers.py    # Pluggable search backends
mcp_server.py          # MCP server (stdio JSON-RPC, 3 tools)
feedback_store.py      # Thread-safe feedback storage
memory_capture.py      # Auto case capture
freshness_rescan.py    # URL liveness + TTL expiry scanner
dedup.py               # AI-powered dedup (scan/clean/merge)
batch_ingest.py        # Bulk topic ingestion for cold-start
eval_batch.py          # Benchmark evaluation (10 questions)
maintenance.py         # Feedback decay + expired case check
metrics.py             # Per-query metrics (JSONL)
Dockerfile             # Container build
docker-compose.yml     # One-click Docker startup
tests/test_core.py     # 22 unit tests
```

## MCP Server

Curator includes a standard MCP server compatible with Claude Desktop, mcporter, and any MCP client:

```bash
python3 mcp_server.py   # Starts stdio JSON-RPC server
```

**Tools:** `curator_query`, `curator_ingest`, `curator_status`

## Testing

```bash
python -m pytest tests/ -v   # 22 tests, all internal (no API calls)
```

## How is this different from LangChain / LlamaIndex?

Those build RAG **pipelines**. Curator governs the **knowledge**:
- Is my existing knowledge good enough?
- Is this new information trustworthy?
- Do my sources contradict each other?
- Has this knowledge gone stale?

Use alongside any RAG framework. It governs knowledge, not pipelines.

## Roadmap

See [ROADMAP.md](ROADMAP.md). Currently at **v0.8**.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
