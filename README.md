# OpenViking Curator

Curator is an **upper-layer intelligence module** for [OpenViking](https://github.com/volcengine/OpenViking).

OpenViking is great at storage/retrieval, but it is passive: what you put in is what you get out.
Curator adds active knowledge governance:

- **Search**: fetch missing external knowledge when local coverage is low
- **Review**: AI quality-check before ingest
- **Score**: assign trust signals for ranking
- **Conflict**: detect contradictory sources (planned)

## Why this project

We want an Agent knowledge base that is:

1. **Self-growing** (learns from real queries)
2. **Quality-controlled** (not just dump everything)
3. **Traceable** (sources and confidence)
4. **Practical** (fast enough for daily use)

## Current status (Pilot)

Implemented in rough v0:

- Scope routing (router model + fallback)
- Local retrieval via OpenViking
- External fallback search via Grok
- AI review and optional ingest-back into OpenViking

Not yet implemented:

- Feedback-driven ranking updates
- Full conflict detection workflow
- Long-term data decay/cleanup policy

## Quick start

```bash
cp .env.example .env
# edit .env and fill your own endpoints/keys
bash run.sh "grok2api auto-register common failures"
```

## Repo structure

- `curator_v0.py` – current pilot script
- `.env.example` – environment template (no secrets)
- `run.sh` – one-command runner with venv bootstrap
- `eval_batch.py` – batch evaluation script

## Roadmap

- **v0.1**: stabilize routing + coverage check + external fallback
- **v0.2**: feedback-driven priority/ranking (feedback store scaffold added)
- **v0.3**: conflict detection
- **v0.4**: memory cleanup and freshness re-scan

## Disclaimer

This is an experimental project under active iteration.
