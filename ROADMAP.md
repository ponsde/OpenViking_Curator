# Roadmap

## Current: v1.0-rc (Governance Layer)

### ✅ Done
- OV-native v2 pipeline (structured output, no answer generation)
- Strict on-demand L0→L1→L2 loading
- LLM call optimization (0-2 calls per run)
- Merged judge + conflict detection (single LLM call)
- Cross-validation (risk tagging, optional)
- Freshness scoring (semantic layer, OV补充)
- Resource dedup scanning (report-only)
- Post-ingest verification
- Decision trace in meta output
- MCP server (HTTP API mode)
- Docker support
- 37 unit tests + CI

### 🔜 Next
- Evaluation framework (fixed benchmark, CI-integrated)
- Demo GIF for README
- PyPI packaging
- OV upstream: propose `POST /sessions/{id}/used` HTTP endpoint

## Future
- Knowledge graph relations
- Auto case pattern recognition
- Multi-agent Curator (shared governance across agents)
