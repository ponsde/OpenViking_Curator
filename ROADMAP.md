# Roadmap

## v0.1
- [x] Scope routing with model fallback
- [x] Local retrieval + coverage gate
- [x] External search fallback
- [x] AI review and ingest-back
- [x] Batch eval script

## v0.2
- [x] Feedback-driven weighting (up/down/adopt) in local retrieval gate

## v0.3
- [x] Conflict detection (top-k scoped)
- [x] Conflict card in final answer

## v0.4
- [x] Decay and cleanup policy (maintenance script)
- [x] Freshness rescan (source-level)
- [x] Deterministic relevance gate

## v0.5
- [x] File locking for feedback store
- [x] curator_query.py one-command entry
- [x] Unit tests (17 initial)
- [x] Full README rewrite (EN/CN)

## v0.6
- [x] Triple-path retrieval (find + search + raw)
- [x] Local keyword index fallback
- [x] Abbreviation expansion
- [x] Curated document boost with overlap verification
- [x] Noise URI filtering

## v0.7
- [x] Rule-based routing (1300x faster)
- [x] Reduced API calls in fast mode
- [x] Chinese tokenization dictionary
- [x] MCP server (stdio JSON-RPC, 3 tools)

## v0.8
- [x] Cross-validation of volatile claims + chain search
- [x] Answer uncertainty marking (⚠️ 待验证)
- [x] External search requires GitHub project activity data
- [x] Ingestion TTL metadata (review_after dates)
- [x] Core keyword vs generic term separation
- [x] Short-word boundary matching (prevent false hits)
- [x] core_cov drives external search trigger
- [x] Cross-validate model fallback chain
- [x] Answer transparency (source footer with coverage)
- [x] Priority_uris semantic filtering
- [x] All thresholds configurable via env
- [x] freshness_rescan.py TTL scan mode
- [x] 22 unit tests

## v0.9 (current)
- [x] Modularization: curator.py (1035 lines) → curator/ package (8 modules)
- [x] `run()` returns structured dict, logging replaces print
- [x] Pluggable search backend (search_providers.py: grok/oai/custom)
- [x] Docker + docker-compose + .dockerignore
- [x] GitHub Actions CI workflow
- [x] 46 unit tests (38→46: freshness + trust scoring)
- [x] Real uri_freshness_score with time-decay (30d/180d/365d thresholds)
- [x] Enhanced uri_trust_score with feedback-weighted adjustment
- [x] eval_batch.py updated for modular architecture
- [x] fcntl Windows compatibility
- [x] CONTRIBUTING.md + MIT LICENSE
- [x] OpenViking memory bridge (long-term memory sync)
- [ ] Soft integration auto-trigger (main model decides when to invoke)
- [ ] Pattern generation from accumulated cases
- [ ] Feedback closed loop (👍👎 after answer → feedback_store)
- [ ] Periodic dedup + TTL cleanup (cron)
- [ ] Knowledge base health dashboard

## v1.0 (target)
- [ ] Long-term memory full migration to OpenViking
- [ ] Multi-source conflict detection
- [ ] Feasibility judgment for dev tasks
- [ ] Knowledge graph from cases/patterns
- [ ] Eval regression suite (fixed 10-question benchmark)
- [ ] External search candidate scoring (best-of-N before ingest)
