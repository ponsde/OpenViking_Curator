# OpenViking Curator

English / [中文](README_CN.md)

**Knowledge governance plugin for [OpenViking](https://github.com/volcengine/OpenViking).** Query → assess → search → ingest → grow. Your OV gets smarter with every question.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green.svg)](https://python.org)

## How it works

```
User asks a question
        ↓
   Query OV first
        ↓
  Coverage enough?  ──yes──→  Return local context (0 LLM calls)
        ↓ no
  External search (Grok / OpenAI / custom)
        ↓
  LLM reviews quality & freshness
        ↓
  Pass? → Ingest back to OV (next time = instant hit)
        ↓
  Return merged context (local + external)
        ↓
  Your LLM answers with full context — one call, done.
```

## What it does

| Feature | Description |
|---------|-------------|
| **Coverage gate** | Trusts OV's score. Sufficient → return. Marginal/low → external search. |
| **External search + auto-ingest** | Grok (default) or any OAI-compatible model. Reviewed results auto-stored to OV. Next query hits locally. |
| **L0→L1→L2 on-demand loading** | Abstract first, overview if needed, full text only when necessary. Saves tokens. |
| **Conflict detection** | Flags contradictions between local and external sources. |
| **Session lifecycle** | Tracks which knowledge was used (`session.used`), extracts long-term memory (`session.commit`). Frequently used knowledge ranks higher. |
| **Query logging + weak topic analysis** | Logs every query. Cluster analysis finds knowledge gaps. Proactive strengthening fills them. |
| **Freshness scanning** | Periodic scan of all resources. Tags fresh/aging/stale. Auto-refresh stale content. |
| **Merged context output** | Returns `context` = local + external combined. Your LLM uses it directly — no second query needed. |

### What Curator does NOT do (OV handles these)

- Retrieval / vector search → OV `find` / `search`
- Content storage / indexing → OV manages
- Memory extraction / dedup → OV `session.commit`
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

## Scripts

| Script | What it does |
|--------|-------------|
| `scripts/analyze_weak.py` | Cluster analysis of weak topics from query log |
| `scripts/strengthen.py` | Proactively runs pipeline on top N weak topics |
| `scripts/freshness_scan.py` | Scans all resources for freshness. `--act` to auto-refresh stale content |

## Project structure

```
curator/
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
tests/                 # Unit tests (77 passing)
```

## Testing

```bash
python -m pytest tests/ -v
```

## Roadmap

- [ ] Fix `active_count` URI format (short URI → full URI matching)
- [ ] LLM intelligent merge for similar resources
- [ ] Periodic cron for `analyze_weak.py` + `freshness_scan.py`
- [ ] Self-optimization: effectiveness tracking + auto-tuning thresholds
- [ ] Batch ingest historical notes into OV
- [ ] OV knowledge base cleanup (deduplicate timestamp-named entries)
- [ ] Weekly knowledge health report

## License

[MIT](LICENSE)
