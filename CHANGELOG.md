# Changelog

All notable changes to OpenViking Curator are documented here.

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
- Moved root-level modules into `curator/` package: `feedback_store.py`, `metrics.py`, `memory_capture.py`, `search_providers.py` — all imports updated to relative
- `.env.example` rewritten: all variables documented with recommended values, threshold explanations, production vs. local guidance
- `CHANGELOG.md` added
- README.md + README_CN.md updated: new features documented, roadmap updated

### Tests
172 tests, all passing. New test files: `test_retrieval_feedback.py`, `test_dedup_enhanced.py`, `test_decision_report.py`.

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
