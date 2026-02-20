# OpenViking Curator

English / [中文](README_CN.md)

**OV 的治理层。** Not just retrieve — decide, verify, and grow. Curator doesn't answer questions — it provides structured, verified context for your LLM.

[![CI](https://github.com/ponsde/OpenViking_Curator/actions/workflows/ci.yml/badge.svg)](https://github.com/ponsde/OpenViking_Curator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

## What is this?

Traditional RAG: put data in → get data out. Curator adds the governance layer:

| Feature | What it does |
|---------|-------------|
| **Coverage Gate** | Measures local retrieval quality (trusting OV's score); only triggers external search when needed |
| **Pluggable External Search** | Grok, OpenAI, or your own backend — just set an env var |
| **AI Review + Ingest** | Reviews external results for quality/freshness before storing back to OV |
| **Cross-Validation** | Tags risky/volatile claims in external results |
| **Conflict Detection** | Identifies contradictions between local and external sources |
| **Case Capture** | Automatically saves Q&A as reusable experience |

### What Curator does NOT do (OV handles these)

- ❌ Retrieval / semantic search (OV `find` / `search`)
- ❌ L0/L1/L2 content loading (OV `abstract` / `overview` / `read`)
- ❌ Memory extraction (OV `session.commit`)
- ❌ Trust/freshness scoring (OV `active_count` + score ranking)
- ❌ Deduplication (OV manages its own knowledge base)
- ❌ Answer generation (caller's LLM does this)

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
Query → Gate (rule-based) → OV Search (session-based, VLM intent analysis)
                                    ↓
                          L0→L1→L2 On-Demand Loading
                          + Coverage Assessment (trust OV score)
                                 ↙         ↘
                           Sufficient?   External Search (pluggable)
                               ↓                    ↓
                          Return as-is      Cross-Validate (tag risks)
                                                     ↓
                                              Review → Ingest back to OV?
                                                     ↓
                                              Conflict Detection
                                                     ↓
                                            Structured Output
                                            { context_text,
                                              external_text,
                                              coverage,
                                              conflict, meta }
```

**5-step pipeline:** Init/Route → OV Retrieve → On-Demand Load + Coverage → External Search (optional) → Conflict Detection + Session Feedback

**Key design principles:**
- Curator returns structured data, NOT generated answers
- Trust OV's score — no re-scoring or re-ranking
- Strict on-demand loading: L0 first, L1 only if needed, L2 only if L1 insufficient
- External search is supplementation, not replacement

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
    return result_text

PROVIDERS["my_search"] = my_search
```

Then set `CURATOR_SEARCH_PROVIDER=my_search`.

## Configuration

All config via environment variables (`.env` file, git-ignored):

| Variable | Required | Description |
|----------|----------|-------------|
| `CURATOR_OAI_BASE` | ✅ | OpenAI-compatible API base URL |
| `CURATOR_OAI_KEY` | ✅ | API key for judge/review |
| `CURATOR_GROK_KEY` | ✅* | Grok API key (*only if using Grok provider) |
| `CURATOR_SEARCH_PROVIDER` | | Search backend: `grok` (default), `oai`, custom |
| `CURATOR_JUDGE_MODELS` | | Model fallback chain for review/validation |
| `OPENVIKING_CONFIG_FILE` | | Path to OpenViking ov.conf |

**No secrets are hardcoded.** All sensitive values come from `.env`.

### OpenClaw model naming note

If you use Curator via OpenClaw, keep `sonnet1m` mapped to:

- `whidsm/【Claude Code】Claude-Sonnet 4-6-1M`

And remove legacy/nonexistent entries like:

- `whidsm/claude-sonnet-4-6-1m`

## Repo Structure

```
curator/               # Core package (governance only)
  config.py            # Env vars, thresholds, HTTP client
  router.py            # Lightweight rule-based routing (domain + freshness)
  retrieval_v2.py      # Strict on-demand L0→L1→L2 loading + coverage assessment
  session_manager.py   # OV HTTP client + persistent session lifecycle
  search.py            # External search + cross-validation (risk tagging)
  review.py            # AI review, ingest to OV, conflict detection
  pipeline_v2.py       # Main 5-step pipeline (returns structured data)
  legacy/              # Archived v1 modules (answer, feedback, dedup, pipeline, retrieval)
curator_query.py       # CLI entry (--help, --status, query)
search_providers.py    # Pluggable search backends (grok/oai/custom)
mcp_server.py          # MCP server (stdio JSON-RPC, 3 tools)
feedback_store.py      # Thread-safe feedback storage
tests/test_core.py     # Unit tests
eval/benchmark.py      # Fair comparison benchmark
```

## MCP Server

Curator includes a standard MCP server compatible with Claude Desktop, mcporter, and any MCP client:

```bash
python3 mcp_server.py   # Starts stdio JSON-RPC server
```

**Tools:** `curator_query`, `curator_ingest`, `curator_status`

## Testing

```bash
python -m pytest tests/ -v
python eval/benchmark.py     # 10-query benchmark (raw OV vs curator v2)
```

## How is this different from LangChain / LlamaIndex?

Those build RAG **pipelines**. Curator governs the **knowledge**:
- Is my existing knowledge good enough?
- Is this new information trustworthy?
- Do my sources contradict each other?

Use alongside any RAG framework. It governs knowledge, not pipelines.

## License

[MIT](LICENSE)
