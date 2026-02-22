# OpenViking Curator

English / [中文](README_CN.md)

**Knowledge governance plugin for [OpenViking](https://github.com/volcengine/OpenViking).** Query → assess → search → ingest → grow. Your OV gets smarter with every question.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

## How it works

```
User asks a question
        ↓
   Query backend (OV / Milvus / Qdrant / ...)
        ↓
  Coverage enough?  ──yes──→  Return local context (0 LLM calls)
        ↓ no
  External search (Grok / Tavily / Brave / OpenAI)
        ↓
  LLM reviews quality & freshness
        ↓
  Conflict?  → Auto-resolve (trust + freshness) or flag for human review
        ↓
  Pass? → Ingest back to backend (next time = instant hit)
        ↓
  Return merged context (local + external)
```

## What it does

| Feature | Description |
|---------|-------------|
| **Pluggable backend** | `KnowledgeBackend` interface — OV is default, any vector store can be plugged in. |
| **Coverage gate** | Trusts backend's score. Sufficient → return. Marginal/low → external search. |
| **External search + auto-ingest** | Grok (default) or any OAI-compatible model. Reviewed results auto-stored. `--review` mode for human-in-the-loop. |
| **L0→L1→L2 on-demand loading** | Abstract first, overview if needed, full text only when necessary. Saves tokens. |
| **Conflict detection + resolution** | Detects contradictions. Auto-resolves based on trust score + freshness, or flags for human review. Configurable: `auto/local/external/human`. |
| **Session lifecycle** | Tracks which knowledge was used, extracts long-term memory. Frequently used knowledge ranks higher. |
| **Query logging + weak topic analysis** | Logs every query. Cluster analysis finds knowledge gaps. Proactive strengthening fills them. |
| **Freshness scanning** | Periodic scan of all resources. Tags fresh/aging/stale. Auto-refresh stale content. |
| **Review mode** | `--review` flag: runs pipeline but doesn't auto-ingest. Human verifies before storing. |

### What Curator does NOT do (backend handles these)

- Retrieval / vector search → backend `find` / `search`
- Content storage / indexing → backend manages
- Memory extraction / dedup → backend (if supported)
- Answer generation → your LLM

## Quick Start

### Prerequisites

- Python 3.10+
- An OpenViking `ov.conf` with embedding + VLM endpoints ([docs](https://github.com/volcengine/OpenViking))
- API key for external search (Grok recommended) and LLM review

### Local install

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ov.conf.example ov.conf   # Fill your embedding + VLM endpoints
cp .env.example .env         # Fill API keys

python3 curator_query.py --status            # Health check
python3 curator_query.py "How to deploy Redis in Docker?"
python3 curator_query.py --review "sensitive topic"   # Don't auto-ingest
```

### Docker (embedded mode)

```bash
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp ov.conf.example ov.conf
cp .env.example .env

docker compose build
docker compose run --rm curator curator_query.py --status
docker compose run --rm curator curator_query.py "How to deploy Redis in Docker?"
```

### MCP Server

```bash
python3 mcp_server.py   # stdio JSON-RPC, compatible with Claude Desktop / mcporter / any MCP client
```

Tools: `curator_query`, `curator_ingest`, `curator_status`

### Python API

```python
from curator.pipeline_v2 import run

result = run("GPT auto-registration script with Selenium")
print(result["context"])       # merged local + external, ready for LLM
print(result["coverage"])      # 0.0 ~ 1.0
print(result["meta"]["ingested"])  # True if new knowledge was stored
```

## Output format

```json
{
  "context": "local results + external results (merged, ready to use)",
  "coverage": 0.68,
  "conflict": {"has_conflict": false, "summary": "", "points": []},
  "meta": {
    "external_triggered": true,
    "ingested": true,
    "llm_calls": 1,
    "used_uris": ["viking://resources/..."],
    "duration": 42.5
  }
}
```

## Configuration

All config via `.env` (git-ignored):

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENVIKING_CONFIG_FILE` | ✅ | Path to your `ov.conf` |
| `OV_DATA_PATH` | | OV data directory (default: `./data`) |
| `CURATOR_OAI_BASE` | ✅ | OpenAI-compatible API base (for LLM review) |
| `CURATOR_OAI_KEY` | ✅ | API key for review |
| `CURATOR_GROK_BASE` | | Grok endpoint (default: `http://127.0.0.1:8000/v1`) |
| `CURATOR_GROK_KEY` | ✅* | Grok API key (*if using Grok search) |
| `OV_BASE_URL` | | Optional: connect to remote OV HTTP serve instead of embedded mode |
| `CURATOR_VERSION` | | Version tag written into ingest metadata |
| `CURATOR_CHAT_RETRY_MAX` | | Chat retry attempts (default: `3`) |
| `CURATOR_CHAT_RETRY_BACKOFF_SEC` | | Retry backoff base seconds (default: `0.6`) |

### Conflict resolution

| `CURATOR_CONFLICT_STRATEGY` | Default | Behavior |
|---|---|---|
| `auto` | ✅ | Trust ≥ 7 + fresh → prefer external. Trust ≤ 3 → prefer local. Otherwise → human review. |
| `local` | | Always prefer local knowledge |
| `external` | | Always prefer external source |
| `human` | | Always flag for human review |

### Search providers (pluggable)

| Provider | Env value | Description |
|----------|-----------|-------------|
| Grok | `grok` (default) | Real-time web search via grok2api |
| OpenAI | `oai` | Any OAI-compatible model with internet |
| Custom | your name | Register in `search_providers.py` |

### Tunable thresholds

| Variable | Default | Meaning |
|----------|---------|---------|
| `CURATOR_THRESHOLD_COV_SUFFICIENT` | 0.55 | Above this = skip external search |
| `CURATOR_THRESHOLD_COV_MARGINAL` | 0.45 | Above this = marginal (still searches) |
| `CURATOR_THRESHOLD_COV_LOW` | 0.35 | Below this = definitely search |

### Ingest metadata (for traceability)

When Curator auto-ingests reviewed external content, it writes metadata alongside content:

- `freshness`: `current|recent|unknown|outdated`
- `ttl_days`: freshness-based TTL
- `ingested`: ingest date (ISO)
- `version`: Curator version tag (`CURATOR_VERSION`)
- `source_urls`: deduplicated source URLs extracted from external text
- `quality_feedback`: judge signals (e.g. trust/reason/conflict summary)

This keeps each ingested document auditable and ready for future quality loops.


| Script | What it does |
|--------|-------------|
| `scripts/analyze_weak.py` | Cluster analysis of weak topics from query log |
| `scripts/strengthen.py` | Proactively runs pipeline on top N weak topics |
| `scripts/freshness_scan.py` | Scans all resources for freshness. `--act` to auto-refresh stale content |

## Project structure

```
curator/
  backend.py           # KnowledgeBackend abstract interface
  backend_ov.py        # OpenViking implementation (embedded + HTTP)
  pipeline_v2.py       # Main 4-step pipeline (returns structured data)
  session_manager.py   # Dual-mode OV client (embedded / HTTP)
  retrieval_v2.py      # L0→L1→L2 loading + coverage assessment
  search.py            # External search + cross-validation
  review.py            # LLM review + ingest + conflict detection
  router.py            # Lightweight rule-based routing
  config.py            # All config (env-overridable thresholds)
  freshness.py         # Time-decay scoring
  dedup.py             # Resource duplicate scanning
  legacy/              # Archived v1 modules
curator_query.py       # CLI entry
mcp_server.py          # MCP server (stdio)
search_providers.py    # Pluggable search backends
scripts/               # Maintenance scripts
tests/                 # Unit tests (90 passing)
```

## Testing

```bash
python -m pytest tests/ -v
```

## Roadmap

- [x] Storage abstraction layer (`KnowledgeBackend` interface)
- [x] Conflict resolution strategy (auto / local / external / human)
- [x] Review mode (`--review` for human-in-the-loop)
- [ ] More search providers (Tavily, Brave Search, SerpAPI)
- [ ] Example backend implementations (Chroma, pgvector)
- [ ] Coverage auto-tuning (track hit rate → adjust thresholds)
- [ ] Integration tests with real OV + API
- [ ] Prometheus / OpenTelemetry metrics export
- [ ] Knowledge health dashboard
- [ ] Batch rollback / trust marking for ingested content

## License

[MIT](LICENSE)
