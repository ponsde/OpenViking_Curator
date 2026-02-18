# OpenViking Curator

English / [中文](README_CN.md)

Curator is an **active knowledge governance layer** for [OpenViking](https://github.com/volcengine/OpenViking).

Traditional RAG systems are passive — what you put in is what you get out. Curator adds intelligence on top:

| Feature | What it does |
|---------|-------------|
| **Coverage Gate** | Measures local retrieval quality; only triggers external search when needed |
| **External Fallback** | Searches via Grok when local knowledge is insufficient |
| **Quality Review** | AI-powered review before ingesting new knowledge |
| **Trust Scoring** | Feedback-weighted ranking with time decay |
| **Conflict Detection** | Identifies contradictions between sources |
| **Freshness Tracking** | Detects stale knowledge and triggers re-verification |

## How is this different from LangChain / LlamaIndex?

Those frameworks build RAG **pipelines** — they help you retrieve and generate.

Curator focuses on knowledge **governance** — the layer that decides:
- Is my existing knowledge good enough, or do I need to search externally?
- Is this new information trustworthy enough to ingest?
- Do my sources contradict each other?
- Has this knowledge gone stale?

You can use Curator alongside any RAG framework. It governs the knowledge, not the pipeline.

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/ponsde/OpenViking_Curator.git
cd OpenViking_Curator
cp .env.example .env
# Edit .env with your own API endpoints and keys

# 2. Install dependencies (Python 3.10+)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run a query
python3 curator_query.py "What is OpenViking and how does it differ from traditional RAG?"
```

### Integration as an MCP-like Tool

`curator_query.py` is designed for programmatic use — call it from your agent/assistant:

```bash
python3 curator_query.py "your question here"
```

**Output (JSON):**
```json
{"routed": false, "reason": "negative_match"}
```
→ Query doesn't need the knowledge base. Handle it normally.

```json
{"routed": true, "answer": "...", "meta": {"coverage": 0.95, "external_triggered": false}}
```
→ Knowledge base answered. Use the `answer` field.

The built-in gate skips casual conversation, simple commands, and follow-up questions automatically.

## Architecture

```
Query → Gate (rule-based) → Route (LLM) → Local Search (OpenViking)
                                              ↓
                                    Coverage + Quality Check
                                         ↙         ↘
                              Sufficient?        External Search (Grok)
                                  ↓                    ↓
                              Answer            Review → Ingest?
                                                       ↓
                                                   Answer
                                                       ↓
                                              Conflict Detection
                                                       ↓
                                                  Case Capture
```

## Repo Structure

| File | Purpose |
|------|---------|
| `curator_v0.py` | Core pipeline (route → search → review → answer) |
| `curator_query.py` | One-command entry point with built-in query gate |
| `feedback_store.py` | Feedback storage with file locking (thread-safe) |
| `metrics.py` | Per-query execution metrics (JSONL) |
| `memory_capture.py` | Auto case capture after each query |
| `eval_batch.py` | Batch evaluation runner |
| `freshness_rescan.py` | Source-level freshness verification |
| `schemas/` | Case and pattern templates |
| `.env.example` | Environment variable template (no secrets) |
| `tests/` | Unit tests (`pytest`) |

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Description |
|----------|----------|-------------|
| `CURATOR_OAI_BASE` | ✅ | OpenAI-compatible API base URL |
| `CURATOR_OAI_KEY` | ✅ | API key for the above |
| `CURATOR_GROK_BASE` | ✅ | Grok search API base URL |
| `CURATOR_GROK_KEY` | ✅ | Grok API key |
| `CURATOR_ROUTER_MODELS` | | Comma-separated model fallback chain for routing |
| `CURATOR_ANSWER_MODELS` | | Comma-separated model fallback chain for answers |
| `CURATOR_JUDGE_MODELS` | | Comma-separated model fallback chain for review |
| `OPENVIKING_CONFIG_FILE` | | Path to OpenViking config file |

**No secrets are hardcoded.** All sensitive values come from `.env` (git-ignored).

## Model Fallback

Router, judge, and answer stages each support a fallback chain:

```
Model A → (503/500?) → Model B → (fail?) → Model C
```

Configure via `CURATOR_ROUTER_MODELS`, `CURATOR_JUDGE_MODELS`, `CURATOR_ANSWER_MODELS`.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan.

**Done:** v0.1–v0.4 (routing, feedback, conflict detection, freshness, model fallback, unit tests)

**Next:** configurable thresholds, storage abstraction layer, CI/CD, pattern synthesis

## Disclaimer

This is an experimental project under active iteration. The upstream dependency [OpenViking](https://github.com/volcengine/OpenViking) is early-stage. Use at your own risk.
