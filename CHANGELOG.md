# Changelog

All notable changes to OpenViking Curator are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.7.0] — 2026-02-27

### Added

- **Feedback adopt time decay** — anti-Matthew effect: old `adopt` signals decay over
  time so stale highly-adopted resources do not crowd out fresh ones
  (`feedback_store.py`, configurable via `CURATOR_FEEDBACK_DECAY_DAYS`)
- **Concurrent search inflight limit** — `CURATOR_SEARCH_MAX_INFLIGHT` semaphore caps
  simultaneous outgoing HTTP calls; zombie-thread starvation prevented by bounded
  `sem.acquire(timeout=...)` fallback
- **Concurrent search cancel signal** — `threading.Event` passed from the join-timeout
  path into `_gather_search`; background coroutine aborts between provider completions
  rather than running until all provider timeouts expire
- **Async ingest tests without `time.sleep`** — replaced all `sleep()` calls with
  `Event.wait()` for deterministic, race-condition-free async tests
- **`assess_coverage` docs + LLM result annotation** — provider outputs now carry
  `source_type="llm_generated"` flag; `format_results()` prepends an unverified notice
  for LLM-synthesised results
- **Domain filter strict mode** — `CURATOR_DOMAIN_FILTER_STRICT=1` rejects results
  whose domain is not in the allowlist (previously only blocked explicitly blocked
  domains)
- **Degradation tracking** — `metrics.py` surfaces LLM call failures and search
  provider failures to callers via `result["degradation"]`; pipeline continues in
  degraded mode rather than hard-failing
- **`sanitize_markdown` hardening** — strips `on*` event handler attributes and
  dangerous URI schemes (`javascript:`, `data:`, `vbscript:`) from markdown output
- **Weekly governance cycle** — `governance.py` runs 5-phase automated maintenance
  (interest analysis, proactive search, freshness scan, flag generation, TTL
  rebalance) on a configurable interval (default 7 days)
- **Governance CLI** — `governance_cli.py` for manual trigger, report viewing, and
  flag resolution (`keep` / `delete` / `adjust` / `ignore`)
- **Interest analyzer** — `interest_analyzer.py` extracts user interests from
  `query_log` + feedback signals to drive proactive knowledge gap filling
- **Governance report** — `governance_report.py` renders ASCII / JSON / HTML reports
- **Session ID validation before reuse** — `backend_ov.py` verifies the persisted
  `session_id` still exists in OV before reusing it (embedded mode guard)
- **Grok / OAI structured JSON parsing** — `_parse_search_results_json()` uses
  `json.JSONDecoder.raw_decode` to reliably extract JSON arrays from LLM responses
  that contain brackets inside field values
- **pytest-cov coverage gate** — CI enforces 70% minimum coverage; `curator/legacy/`
  omitted from measurement
- **Force external search on empty `load_context`** — pipeline falls through to
  external search when L0/L1/L2 loading returns empty content even if coverage score
  was above threshold

### Changed

- Time-sensitive keywords unified to a single source of truth in
  `router_config.json`; both `router.py` and `search_providers.py` read from it
- `assess_coverage` magic numbers extracted to named constants
- `SearchResult` renamed to `WebSearchResult` in `search_providers.py`
  (backward-compatible alias kept); `_extract_json` used in `_auto_summarize`

### Fixed

- Semaphore acquire with timeout prevents zombie threads from permanently holding
  inflight slots
- URI collision in async job tracking
- Retry off-by-one (was retrying one extra time beyond `max_retries`)
- Scheduler interval validation rejects non-positive values
- Circuit breaker permanent-error classification (was incorrectly marking transient
  errors as permanent)
- `governance.py` flag lock domain and null coverage defense
- `fcntl` guard for non-POSIX platforms; log rotation on Windows
- Judge error classification (transient vs permanent)

### Tests

437 passed, 4 subtests. New: `test_interest_analyzer.py`, `test_governance.py`,
`test_governance_report.py`.

---

## [0.6.0] — 2026-02-26

### Added

- **Structured logging context binding** — every pipeline run binds `run_id`,
  `query_prefix`, `session_id` to structlog contextvars; background async-ingest
  threads inherit context via `copy_context()` for full log correlation under
  `CURATOR_JSON_LOGGING=1`
- **mypy type checking** — zero errors across 28 source files; added to CI workflow
  and pre-commit hooks; `types-requests` stubs included
- **Pydantic Settings v2 — dedup migration** — last `env()` call migrated;
  `CURATOR_DEDUP_SIMILARITY`, `CURATOR_DEDUP_MAX_ITEMS`, `CURATOR_DEDUP_LOG` now
  fully validated by `CuratorSettings`
- **PyPI publish readiness** — `pyproject.toml` with setuptools backend, `py.typed`
  marker, `_version.py`, entry points (`curator-query`, `curator-mcp`)
- **Query-level search cache** — `search_cache.py` with TTL, freshness TTL, max
  entries, and LRU eviction; avoids redundant external calls for repeated queries
- **Circuit breaker** — per-model breaker wrapping `config.chat()` — configurable
  threshold, recovery window, and half-open probe
- **Pydantic Settings v2** — `settings.py` with `BaseSettings`; typed and validated
  configuration replaces raw `env()` calls throughout
- **Structured logging** — `logging_setup.py` structlog bridge; JSON output
  (Loki/Prometheus compatible) toggled via `CURATOR_JSON_LOGGING=1`
- **JSON and HTML export for decision report** — `format_report(fmt="json")` and
  `format_report(fmt="html")` in addition to ASCII
- **Background scheduler** — `scheduler.py` runs periodic maintenance jobs
  (`strengthen`, `freshness_scan`, `ttl_rebalance`) with configurable intervals
- **Docker support** — `Dockerfile` for both embedded and HTTP OV modes
- **uv lock file** — CI migrated from pip to uv for reproducible installs

### Changed

- `JudgeResult` fallback constructors use `model_validate()` instead of `**dict`
  (Pydantic v2 idiom)
- `_run_impl` split into thin context-binding wrapper + `_run_impl_inner` for clean
  `try/finally` semantics
- `result["run_id"]` added to pipeline output for caller log correlation

### Fixed

- Harden scheduler against invalid config and malformed `weak_topics.json`
- Address 二号 review: score `None` guard + URI pop-on-success pattern
- Minor robustness fixes: CB permanent error, null safety, bare key handling, dead
  code removal

### Tests

430+ tests. New: `test_circuit_breaker.py`, `test_search_cache.py`,
`test_packaging.py`, `test_settings.py`, `test_logging_setup.py`.

---

## [0.5.0] — 2026-02-25

### Added

- **Async ingest (fire-and-forget)** — `CURATOR_ASYNC_INGEST=1` queues ingest
  operations in a background thread; pipeline returns immediately without waiting
  for storage
- **Async job tracking + recovery CLI** — `async_jobs.py` persists job state;
  `scripts/async_job_cli.py` lists / filters / replays failed or retryable jobs
- **Query log v2** — structured schema with `run_id`, `coverage`, `external_used`,
  `judge_trust`; `scripts/analyze_query_log.py` aggregation script
- **Coverage tuning script** — `scripts/coverage_tuning.py` (read-only) suggests
  threshold adjustments based on historical query log data
- **Bidirectional conflict resolution** — `review.py` considers both local signals
  (low-trust existing content) and external signals (high-trust new content) when
  choosing the winning version
- **Eval regression benchmark** — fixed query set run against `InMemoryBackend`;
  `eval/benchmark.py` produces a deterministic pass/fail score

### Tests

369+ tests. New: `test_async_ingest.py`, `test_async_jobs.py`,
`test_async_job_cli.py`, `test_eval_regression.py`, `test_conflict_resolution.py`,
`test_query_log.py`, `test_coverage_tuning.py`.

---

## [0.4.0] — 2026-02-25

### Added

- **Domain whitelist / blacklist** — `domain_filter.py` filters external search
  results by `CURATOR_ALLOWED_DOMAINS` and `CURATOR_BLOCKED_DOMAINS`
- **Usage-based TTL** — `usage_ttl.py` promotes/demotes resources between hot, warm,
  and cold TTL tiers based on access frequency; `scripts/ttl_rebalance.py` reports
  and optionally applies tier changes
- **Concurrent search mode** — `search_concurrent()` fires all configured providers
  simultaneously and returns the fastest non-empty result
  (`CURATOR_SEARCH_CONCURRENT=1`)
- **L0/L1 auto-summary on ingest** — `CURATOR_AUTO_SUMMARIZE=1` generates `abstract`
  and `overview` summaries at ingest time so L0/L1 retrieval is available immediately
- **Pre-commit + ruff** — added to the project; all existing lint issues resolved

### Tests

New: `test_domain_filter.py`, `test_usage_ttl.py`, `test_auto_summarize.py`.

---

## [0.3.0] — 2026-02-25

### Added

- **Multi search provider support** — pluggable fallback chain: `grok`, `duckduckgo`
  (no API key), `tavily`; configured via `CURATOR_SEARCH_PROVIDERS` comma-separated
  list; first non-empty result wins
- **Pending review CLI** — `curator/review_cli.py` — `list`, `approve`, `reject`
  operations on items written to `pending_review.jsonl` by the `human` conflict
  strategy

### Tests

New: `test_search_providers.py`, `test_review_cli.py`.

---

## [0.2.0] — 2026-02-25

### Changed

- Cross-review with 二号 (R1 + R2 rounds): fixes for `_truncate_to` exact-fit edge
  case, `cv_warnings` routing via `sys_prompt` instead of `external_text` suffix

---

## [0.1.0] — 2026-02-25

Initial public release.

### Added

**Feedback-driven retrieval ranking**
- `curator/feedback_store.py`: per-URI `up` / `down` / `adopt` signal store with
  file locking; signals persist across restarts
- `rerank_with_feedback()` in `retrieval_v2.py`: adjusts OV retrieval scores by
  `±FEEDBACK_WEIGHT` (default max delta 0.10; OV original score remains dominant)
- Pipeline auto-records `adopt` for every URI consumed by `load_context()`
- `ov_retrieve()` returns both `all_items_raw` (original OV scores) and `all_items`
  (feedback-adjusted) so `assess_coverage` uses unbiased scores for external-search
  decisions

**Enhanced deduplication**
- Layer 1 — URL hash: extracts source URLs, compares MD5 hash sets for instant
  duplicate detection when source URLs overlap (`method="url_hash"`)
- Layer 2 — Jaccard word similarity: word-set `|A∩B|/|A∪B|`; order-invariant, no
  external dependencies (`method="jaccard"`)
- CJK single-character tokens preserved (`\u4e00–\u9fff`)
- `max_checks` default changed from `5` to `0` (auto-adaptive:
  `min(50, len(uris) * 3)`)
- Duplicate reports include `method` field

**Decision Report**
- `curator/decision_report.py` with `format_report()` and `format_report_short()`
- CJK-safe ASCII box formatting using `unicodedata.east_asian_width` for correct
  terminal alignment with Chinese / Japanese / Korean content
- Automatically included in every `pipeline_v2.run()` return as
  `result["decision_report"]`; partial reports generated on early-return paths

**Project structure**
- `pyproject.toml` added: setuptools packaging, dependencies, dev extras, pytest
  config
- Root-level modules moved into `curator/` package
- `.env.example` rewritten with full documentation
- `README.md` and `README_CN.md` updated

### Tests

172 tests. New: `test_retrieval_feedback.py`, `test_dedup_enhanced.py`,
`test_decision_report.py`.

---

## [pre-0.1.0] — 2026-02

Initial development iterations (not released to PyPI).

### Established foundations

- `KnowledgeBackend` ABC + `OpenVikingBackend` / `InMemoryBackend` implementations
- Dual-path retrieval (`find` + session `search`), L0→L1→L2 on-demand loading
- Coverage assessment + external search trigger logic
- Judge + conflict detection (single merged LLM call), configurable resolution
  strategies (`auto` / `local` / `external` / `human`)
- Ingest with metadata (`source_urls`, `version`, `quality_feedback`, TTL)
- Freshness scanning + TTL management (`freshness.py`)
- Weak topic analysis + proactive strengthening (`scripts/analyze_weak.py`,
  `scripts/strengthen.py`)
- Session manager — query tracking + long-term memory extraction
- Query logging and case capture templates

---

[Unreleased]: https://github.com/ponsde/OpenViking_Curator/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/ponsde/OpenViking_Curator/compare/v0.1.0...v0.7.0
[0.1.0]: https://github.com/ponsde/OpenViking_Curator/releases/tag/v0.1.0
