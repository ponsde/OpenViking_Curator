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
- Phase 1 知识积累闭环（补强自动化、定期分析 cron）
- Evaluation framework (fixed benchmark, CI-integrated)
- Demo GIF for README
- PyPI packaging
- OV upstream: propose `POST /sessions/{id}/used` HTTP endpoint

### ✅ Phase 1: 知识积累闭环
- Query 日志（`_log_query` in pipeline_v2.py → data/query_log.jsonl）
- 弱点分析脚本（scripts/analyze_weak.py）
- 主动补强脚本（scripts/strengthen.py）
- 8 个新增单元测试（45 total, 0 regression）

## Future
- Knowledge graph relations
- Auto case pattern recognition
- Multi-agent Curator (shared governance across agents)
