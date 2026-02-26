# Changelog

All notable changes to OpenViking Curator are documented here.

---

## [v0.7.0] — 2026-02-26

### Added
- **PyPI publish readiness**: `pyproject.toml` with setuptools, `py.typed`, `_version.py`, entry points
- **Query-level search cache**: `search_cache.py` with TTL, freshness TTL, max entries, LRU eviction
- **Circuit breaker**: per-model breaker wrapping `config.chat()` — threshold, recovery, half-open probe
- **Pydantic Settings v2**: `settings.py` — typed, validated config with `BaseSettings`
- **Structured logging**: `logging_setup.py` — structlog bridge with JSON / console toggle

### Tests
430+ tests. New: `test_circuit_breaker.py`, `test_search_cache.py`, `test_packaging.py`, `test_settings.py`, `test_logging_setup.py`.

---

## [v0.6.0] — 2026-02-26

### Added
- **Configurable judge prompt**: external `.prompt` template file support
- **Configurable dedup**: `CURATOR_DEDUP_SCAN_LIMIT`, `CURATOR_DEDUP_THRESHOLD`
- **Structured search results**: `SearchResult` dataclass replaces raw strings
- **Feedback adopt**: score threshold + rank weighting for auto-adopt
- **Coverage assessment**: score gap + keyword overlap signals
- **Router config**: domain/time keywords configurable via `router_config.json`
- **CuratorPipeline class**: reusable backend/session + health TTL
- **File locking**: advisory file locking for concurrent writes

### Fixed
- Judge prompt template escaping + coverage clamp to `[0, 1]` + dedup safe parse
- Replace 17 bare `except Exception` blocks with logged exceptions
- Async ingest test race condition (wait inside patch scope)

### Changed
- Default models updated to `gpt-5.3-codex` + `Claude-Sonnet 4-6`
- Split `requirements.txt`, pin versions to minor range
- Eliminate duplicate `env()`/`_chat()` in `search_providers`

### Tests
417 tests.

---

## [v0.5.0] — 2026-02-25

### Added
- **Async ingest**: fire-and-forget mode with job tracking + recovery CLI
- **Query log v2**: structured schema + aggregation script
- **Coverage tuning**: suggestion script (read-only)
- **Bidirectional conflict resolution**: local signals + scores
- **Eval regression**: benchmark with fixed query set, `InMemoryBackend`

### Tests
369+ tests. New: `test_async_ingest.py`, `test_async_jobs.py`, `test_async_job_cli.py`, `test_eval_regression.py`, `test_conflict_resolution.py`, `test_query_log.py`, `test_coverage_tuning.py`.

---

## [v0.4.0] — 2026-02-25

### Added
- **Domain whitelist/blacklist**: `domain_filter.py` for external search results
- **Usage-based TTL**: hot/warm/cold tiers in `usage_ttl.py` + `ttl_rebalance.py` script
- **Concurrent search mode**: fire all providers in parallel, take fastest non-empty result
- **L0/L1 auto-summary on ingest**: `CURATOR_AUTO_SUMMARIZE` opt-in

### Changed
- Pre-commit + ruff added, all lint issues fixed

### Tests
New: `test_domain_filter.py`, `test_usage_ttl.py`, `test_auto_summarize.py`.

---

## [v0.3.0] — 2026-02-25

### Added
- **Multi search provider support**: pluggable fallback chain (`grok`, `duckduckgo`, `tavily`)
- **Pending review CLI**: `review_cli.py` — approve/reject/list pending reviews

### Tests
New: `test_search_providers.py`, `test_review_cli.py`.

---

## [v0.2.0] — 2026-02-25

### Added
- **Code quality R1 + R2**: cross-review with 二号, fixes for `_truncate_to` exact-fit, `cv_warnings` via sys_prompt

---

## [v0.1.0] — 2026-02-25

### Added

**Feedback-driven retrieval ranking**
- `curator/feedback_store.py`: per-URI `up`/`down`/`adopt` signal store with file-locking
- `rerank_with_feedback()` in `retrieval_v2.py`: adjusts OV retrieval scores by ±`FEEDBACK_WEIGHT` (default 0.10 max delta, OV original score stays dominant)
- Pipeline auto-records `adopt` for each URI actually used by `load_context`
- `ov_retrieve()` now returns `all_items_raw` (OV original scores) alongside `all_items` (feedback-adjusted), ensuring `assess_coverage` uses original scores for external-search decisions

**Enhanced deduplication**
- Layer 1 — URL hash: extracts source URLs, compares md5 hash sets → instant duplicate detection when source URLs overlap, `method="url_hash"`
- Layer 2 — Jaccard word similarity: replaces SequenceMatcher; word-set `|A∩B|/|A∪B|`, order-invariant, no external deps, `method="jaccard"`
- CJK single-character tokens preserved (`\u4e00–\u9fff`) — single Chinese characters have discriminative value in technical docs
- `max_checks` default changed from `5` to `0` (auto-adaptive: `min(50, len(uris) * 3)`)
- Duplicate reports now include `method` field

**Decision Report**
- `curator/decision_report.py` with `format_report()` and `format_report_short()`
- `format_report()`: CJK-safe ASCII box summary using `unicodedata.east_asian_width` for correct terminal alignment with Chinese/Japanese/Korean content
- `format_report_short()`: single-line log-friendly format
- Automatically included in every `pipeline_v2.run()` return as `result["decision_report"]`
- Pipeline early-return paths (e.g. OV init failure) also generate a partial report

**Project structure**
- `pyproject.toml` added: setuptools packaging, deps, dev extras, pytest config
- Moved root-level modules into `curator/` package
- `.env.example` rewritten with full documentation
- README.md + README_CN.md updated

### Tests
172 tests. New: `test_retrieval_feedback.py`, `test_dedup_enhanced.py`, `test_decision_report.py`.

---

## [pre-v0.1.0] — 2026-02

Initial development iterations:
- KnowledgeBackend abstraction + OpenVikingBackend / InMemoryBackend
- Dual-path retrieval (find + session search), L0→L1→L2 layered loading
- Coverage assessment + external search trigger
- Judge + conflict detection (single LLM call), configurable resolution strategies
- Ingest with metadata (source_urls, version, quality_feedback, TTL)
- Freshness scanning + TTL management
- Weak topic analysis + proactive strengthening
- Session manager (query tracking + long-term memory extraction)
- Query logging, case capture
