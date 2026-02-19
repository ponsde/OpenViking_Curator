# OpenViking Curator

English / [中文](README_CN.md)

**OV 的外搜补充层 + 治理层。** Not just retrieve — decide, verify, and grow. Curator doesn't answer questions — it provides structured, verified context for your LLM.

[![CI](https://github.com/ponsde/OpenViking_Curator/actions/workflows/ci.yml/badge.svg)](https://github.com/ponsde/OpenViking_Curator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

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
{
  "routed": true,
  "context_text": "...",
  "external_text": "",
  "coverage": 0.7,
  "conflict": {"has_conflict": false},
  "meta": {"external_triggered": false, "used_uris": [...]}
}
```

## Architecture

```
Query → Gate (rule+LLM) → Scope Router → OV Search (session-based)
                                              ↓
                                    L0→L1→L2 Layered Loading
                                    + Coverage Assessment
                                         ↙         ↘
                                   Sufficient?   External Search (pluggable)
                                       ↓                    ↓
                                  Structured       Cross-Validate → Review → Ingest?
                                   Output                                    ↓
                                       ↓                              Conflict Detection
                                  { context_text,                          ↓
                                    external_text,               Structured Output
                                    coverage,
                                    conflict, meta }
```

**5-step pipeline:** Init/Route → OV Retrieve → Layered Load + Coverage → External Search (optional) → Conflict Detection + Session Feedback

**Key design:** Curator returns structured data (context, coverage, conflicts), NOT generated answers. The caller decides how to use this context with their own LLM.

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
curator/               # Core package (modular)
  config.py            # Env vars, thresholds, HTTP client
  router.py            # Rule-based + LLM scope routing
  retrieval_v2.py      # OV-native L0→L1→L2 retrieval + coverage
  session_manager.py   # OV HTTP client + persistent session lifecycle
  feedback.py          # Trust/freshness scoring, feedback ranking
  search.py            # External search + cross-validation
  review.py            # AI review, ingest, conflict detection
  answer.py            # Answer generation (optional, caller decides)
  pipeline_v2.py       # Main 5-step pipeline (returns structured data)
  legacy/              # v1 modules (kept for reference)
curator_query.py       # CLI entry (--help, --status, query)
search_providers.py    # Pluggable search backends (grok/oai/custom)
mcp_server.py          # MCP server (stdio JSON-RPC, 3 tools)
feedback_store.py      # Thread-safe feedback storage
freshness_rescan.py    # URL liveness + TTL expiry scanner
dedup.py               # AI-powered dedup (scan/clean/merge)
batch_ingest.py        # Bulk topic ingestion for cold-start
eval/benchmark.py      # Fair comparison benchmark (retrieval content only)
tests/test_core.py     # 50 unit tests
```

## MCP Server

Curator includes a standard MCP server compatible with Claude Desktop, mcporter, and any MCP client:

```bash
python3 mcp_server.py   # Starts stdio JSON-RPC server
```

**Tools:** `curator_query`, `curator_ingest`, `curator_status`

## Testing

```bash
python -m pytest tests/ -v   # 50 tests, all internal (no API calls)
python eval/benchmark.py     # 10-query benchmark (raw OV vs curator v2, fair retrieval comparison)
python eval/deadlock_repro.py --mode both  # embedded vs HTTP deadlock check
```

## How is this different from LangChain / LlamaIndex?

Those build RAG **pipelines**. Curator governs the **knowledge**:
- Is my existing knowledge good enough?
- Is this new information trustworthy?
- Do my sources contradict each other?
- Has this knowledge gone stale?

Use alongside any RAG framework. It governs knowledge, not pipelines.

## Roadmap

See [ROADMAP.md](ROADMAP.md). Currently at **v0.9** (OV-native v2 pipeline).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
