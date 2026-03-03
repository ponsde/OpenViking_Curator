"""Governance phase implementations (Phase 0-5)."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .config import CURATED_DIR, env, log
from .governance_audit import load_audit_log, write_audit
from .governance_flags import create_flag, expire_flags, load_flags
from .governance_traces import (
    TRACE_DONE,
    TRACE_FAILED,
    TRACE_QUEUED,
    write_trace_event,
)


def _analyze_weak_topics(data_path: str, min_queries: int = 2) -> list[dict]:
    """Delegate to the shared nlp_utils implementation."""
    from .nlp_utils import analyze_weak_topics

    return analyze_weak_topics(data_path, min_queries=min_queries)


def _aggregate_query_metrics(data_path: str) -> dict:
    """Inline query log aggregation (same logic as scripts/query_log_aggregate.py)."""
    log_path = os.path.join(data_path, "query_log.jsonl")
    if not os.path.exists(log_path):
        return {"total_queries": 0}

    entries: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "query" in entry:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue

    n = len(entries)
    if n == 0:
        return {"total_queries": 0}

    coverages = [float(e.get("coverage") or 0) for e in entries]
    external_count = sum(1 for e in entries if e.get("external_triggered"))
    ingest_count = sum(1 for e in entries if e.get("ingested"))
    conflict_count = sum(1 for e in entries if e.get("has_conflict"))
    llm_calls = [e.get("llm_calls", 0) for e in entries]

    return {
        "total_queries": n,
        "coverage": {
            "mean": round(sum(coverages) / n, 4),
            "min": round(min(coverages), 4),
            "max": round(max(coverages), 4),
        },
        "rates": {
            "external_triggered": round(external_count / n, 4),
            "ingested": round(ingest_count / n, 4),
            "has_conflict": round(conflict_count / n, 4),
        },
        "llm_calls": {
            "mean": round(sum(llm_calls) / n, 2),
            "total": sum(llm_calls),
        },
    }


def _freshness_scan_backend(backend: Any) -> dict[str, list[dict]]:
    """Backend-agnostic freshness scan.  Returns {fresh, aging, stale} lists."""
    from .config import AGING_THRESHOLD, FRESH_THRESHOLD
    from .freshness import uri_freshness_score

    cats: dict[str, list[dict]] = {"fresh": [], "aging": [], "stale": []}
    if not hasattr(backend, "list_resources"):
        return cats

    uris = backend.list_resources()
    if not uris:
        return cats

    for uri in uris:
        score = uri_freshness_score(uri)
        if score >= FRESH_THRESHOLD:
            category = "fresh"
        elif score >= AGING_THRESHOLD:
            category = "aging"
        else:
            category = "stale"
        cats[category].append({"uri": uri, "score": score, "category": category})

    return cats


def _ttl_rebalance_scan(curated_dir: str) -> list[dict]:
    """Scan curated markdown backups for TTL adjustment suggestions."""
    from .feedback_store import load as load_feedback
    from .usage_ttl import adjust_ttl, usage_tier

    if not os.path.isdir(curated_dir):
        return []

    feedback = load_feedback()
    results: list[dict] = []

    for fname in os.listdir(curated_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(curated_dir, fname)

        # Parse curator_meta from first 500 chars
        try:
            with open(fpath, encoding="utf-8") as f:
                head = f.read(500)
        except OSError:
            continue

        # Extract freshness/TTL from meta comment
        meta_match = re.search(r"<!--\s*curator_meta:\s*(.+?)\s*-->", head)
        meta: dict[str, str] = {}
        if meta_match:
            for pair in meta_match.group(1).split():
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    meta[k] = v

        current_freshness = meta.get("freshness", "unknown")
        try:
            current_ttl = int(meta.get("ttl_days", "90"))
        except (ValueError, TypeError):
            current_ttl = 90

        # Find feedback for this file.
        # Match filename stem as a path segment (after / or as basename).
        stem = fname.replace(".md", "")
        adopt_count = 0
        has_feedback = False
        for uri, fb in feedback.items():
            # Exact basename or path-segment match to avoid substring false positives
            if uri.endswith(fname) or uri.endswith(stem) or f"/{stem}/" in uri:
                has_feedback = True
                adopt_count += fb.get("adopt", 0)

        # Skip files with no feedback data -- absence of feedback != unused.
        # Only flag resources that have actual usage signals pointing to cold.
        if not has_feedback:
            continue

        current_tier = usage_tier(adopt_count)
        suggested_ttl = adjust_ttl(current_ttl, current_tier)
        changed = suggested_ttl != current_ttl

        if changed:
            results.append(
                {
                    "file": fname,
                    "freshness": current_freshness,
                    "current_ttl": current_ttl,
                    "adopt_count": adopt_count,
                    "current_tier": current_tier,
                    "suggested_ttl": suggested_ttl,
                    "delta_days": suggested_ttl - current_ttl,
                    "changed": True,
                }
            )

    return results


def phase1_collect(
    data_path: str,
    lookback_days: int,
    cycle_id: str,
    mode: str,
) -> dict:
    """Phase 1: Data collection -- read query log, feedback, weak topics."""
    from .interest_analyzer import extract_interests

    # Weak topics
    weak_topics = _analyze_weak_topics(data_path)
    write_audit(
        cycle_id=cycle_id,
        phase="collect",
        action="analyze_weak",
        outcome=f"found_{len(weak_topics)}",
        mode=mode,
        data_path=data_path,
    )

    # Query metrics
    query_metrics = _aggregate_query_metrics(data_path)
    write_audit(
        cycle_id=cycle_id,
        phase="collect",
        action="query_metrics",
        outcome=f"queries_{query_metrics.get('total_queries', 0)}",
        mode=mode,
        data_path=data_path,
    )

    # User interests
    interests = extract_interests(
        data_path=data_path,
        lookback_days=lookback_days,
    )
    write_audit(
        cycle_id=cycle_id,
        phase="collect",
        action="extract_interests",
        outcome=f"topics_{len(interests)}",
        mode=mode,
        data_path=data_path,
    )

    return {
        "weak_topics": weak_topics,
        "query_metrics": query_metrics,
        "interests": interests,
    }


def phase2_audit(
    data_path: str,
    cycle_id: str,
    mode: str,
    backend: Any = None,
) -> dict:
    """Phase 2: Database audit -- freshness, TTL rebalance, retryable jobs."""
    from .async_jobs import get_retryable_jobs

    result: dict[str, Any] = {
        "freshness": {"fresh": [], "aging": [], "stale": []},
        "url_checks": {},
        "ttl_suggestions": [],
        "retryable_jobs": [],
    }

    # Freshness scan (backend-agnostic)
    if backend is not None:
        try:
            result["freshness"] = _freshness_scan_backend(backend)
            total = sum(len(v) for v in result["freshness"].values())
            write_audit(
                cycle_id=cycle_id,
                phase="audit",
                action="freshness_scan",
                outcome=f"scanned_{total}",
                mode=mode,
                data_path=data_path,
            )
        except Exception as e:
            log.warning("governance.phase2: freshness scan error: %s", e)
            write_audit(
                cycle_id=cycle_id,
                phase="audit",
                action="freshness_scan",
                outcome="error",
                details={"error": str(e)[:200]},
                mode=mode,
                data_path=data_path,
            )
    else:
        log.debug("governance.phase2: no backend, skipping freshness scan")

    # TTL rebalance
    try:
        curated_dir = env("CURATOR_CURATED_DIR", "") or CURATED_DIR
        suggestions = _ttl_rebalance_scan(curated_dir)
        result["ttl_suggestions"] = suggestions
        if suggestions:
            write_audit(
                cycle_id=cycle_id,
                phase="audit",
                action="ttl_rebalance",
                outcome=f"suggestions_{len(suggestions)}",
                mode=mode,
                data_path=data_path,
            )
    except Exception as e:
        log.warning("governance.phase2: ttl rebalance error: %s", e)

    # Retryable async jobs
    try:
        retryable = get_retryable_jobs()
        result["retryable_jobs"] = retryable
        if retryable:
            write_audit(
                cycle_id=cycle_id,
                phase="audit",
                action="retryable_jobs",
                outcome=f"found_{len(retryable)}",
                mode=mode,
                data_path=data_path,
            )
    except Exception as e:
        log.warning("governance.phase2: retryable jobs error: %s", e)

    return result


def phase3_flag(
    data_path: str,
    cycle_id: str,
    mode: str,
    audit_data: dict,
) -> list[dict]:
    """Phase 3: Soft flagging -- dedup against existing, expire stale first."""
    from .config import FLAG_EXPIRE_DAYS

    # Expire stale pending flags before creating new ones
    expire_days = FLAG_EXPIRE_DAYS
    if expire_days > 0:
        expired = expire_flags(data_path=data_path, expire_days=expire_days)
        if expired:
            log.info("governance.phase3: expired %d stale pending flags", len(expired))
            write_audit(
                cycle_id=cycle_id,
                phase="flag",
                action="expire_flags",
                outcome=f"expired_{len(expired)}",
                mode=mode,
                data_path=data_path,
            )

    # Build set of (uri, flag_type) for existing unresolved flags
    existing_flags = load_flags(data_path=data_path)
    active_keys: set[tuple[str, str]] = {
        (f.get("uri", ""), f.get("flag_type", "")) for f in existing_flags if f.get("status") == "pending"
    }

    flags_created: list[dict] = []

    def _maybe_flag(uri: str, flag_type: str, **kwargs) -> dict | None:
        """Create flag only if no active flag exists for this (uri, flag_type)."""
        key = (uri, flag_type)
        if key in active_keys:
            return None
        flag = create_flag(cycle_id=cycle_id, uri=uri, flag_type=flag_type, data_path=data_path, **kwargs)
        active_keys.add(key)
        return flag

    # Flag stale resources
    for res in audit_data.get("freshness", {}).get("stale", []):
        uri = res.get("uri", "")
        score = res.get("score", 0)
        flag = _maybe_flag(
            uri,
            "stale_resource",
            severity="high" if score < 0.2 else "medium",
            reason=f"Freshness score {score:.2f} (below stale threshold)",
            details={"score": score, "category": "stale"},
        )
        if flag:
            flags_created.append(flag)

    # Flag broken URLs
    for uri, checks in audit_data.get("url_checks", {}).items():
        broken = [c for c in checks if not c.get("ok")]
        for bc in broken:
            flag = _maybe_flag(
                uri,
                "broken_url",
                severity="medium",
                reason=f"URL unreachable: {bc.get('url', '')[:80]}",
                details={"url": bc.get("url", ""), "status": bc.get("status", 0)},
            )
            if flag:
                flags_created.append(flag)

    # Flag review-expired resources (across all freshness categories)
    for cat_name in ("fresh", "aging", "stale"):
        for res in audit_data.get("freshness", {}).get(cat_name, []):
            if res.get("review_expired"):
                flag = _maybe_flag(
                    res.get("uri", ""),
                    "review_expired",
                    severity="low",
                    reason=f"Review date expired: {res.get('review_after', '?')}",
                    details={"review_after": res.get("review_after")},
                )
                if flag:
                    flags_created.append(flag)

    # Flag TTL rebalance suggestions
    for suggestion in audit_data.get("ttl_suggestions", []):
        flag = _maybe_flag(
            suggestion.get("file", ""),
            "ttl_rebalance",
            severity="low",
            reason=(
                f"TTL adjust: {suggestion.get('current_tier', '?')}"
                f" {suggestion.get('current_ttl', '?')}d"
                f" → {suggestion.get('suggested_ttl', '?')}d"
                f" ({suggestion.get('delta_days', 0):+d} days)"
            ),
            details=suggestion,
        )
        if flag:
            flags_created.append(flag)

    if flags_created:
        write_audit(
            cycle_id=cycle_id,
            phase="flag",
            action="create_flags",
            outcome=f"created_{len(flags_created)}",
            mode=mode,
            data_path=data_path,
        )

    return flags_created


def _run_async_governance_batch(
    items: list[dict],
    data_path: str,
    cycle_id: str,
    mode: str,
    run_fn: Callable,
    backend: Any = None,
) -> None:
    """Run governance searches in background thread, writing trace events."""
    for item in items:
        trace_id = item["trace_id"]
        query = item.get("query", "")
        if not query:
            write_trace_event(data_path, trace_id, TRACE_FAILED, error="empty query")
            continue
        try:
            kwargs: dict[str, Any] = {"auto_ingest": True}
            if backend is not None:
                kwargs["backend"] = backend
            result = run_fn(query, **kwargs)
            ingested = (result.get("meta") or {}).get("ingested", False)
            coverage = (result.get("meta") or {}).get("coverage", 0)
            write_trace_event(
                data_path,
                trace_id,
                TRACE_DONE,
                result={"ingested": ingested, "coverage": coverage, "query": query},
            )
            write_audit(
                cycle_id=cycle_id,
                phase="proactive_async",
                action=f"async_{item.get('job_type', 'search')}",
                target=query,
                outcome="ingested" if ingested else "no_new_content",
                details={"trace_id": trace_id, "coverage": coverage},
                mode=mode,
                data_path=data_path,
            )
        except Exception as e:
            log.warning("governance.async_batch: error query=%s: %s", query[:50], e)
            write_trace_event(
                data_path,
                trace_id,
                TRACE_FAILED,
                error=str(e)[:200],
                query=query,
            )

    log.info("governance.async_batch: completed %d items for cycle %s", len(items), cycle_id)


def _resolve_run_fn(run_fn: Callable | None) -> Callable:
    if run_fn is not None:
        return run_fn
    from .pipeline_v2 import run as _pipeline_run

    return _pipeline_run


def _get_sync_budget() -> int:
    try:
        return max(0, int(env("CURATOR_GOVERNANCE_SYNC_BUDGET", "0")))
    except (ValueError, TypeError):
        return 0


def _load_existing_queries(data_path: str) -> set[str]:
    existing: set[str] = set()
    ql_path = os.path.join(data_path, "query_log.jsonl")
    if not os.path.exists(ql_path):
        return existing
    with open(ql_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                existing.add(entry.get("query", "").lower())
            except json.JSONDecodeError:
                continue
    return existing


def _run_sync_proactive_queries(
    sync_queries: list,
    fn: Callable,
    result: dict[str, Any],
    cycle_id: str,
    mode: str,
    data_path: str,
    backend: Any = None,
) -> None:
    for pq in sync_queries:
        try:
            kwargs: dict[str, Any] = {"auto_ingest": True}
            if backend is not None:
                kwargs["backend"] = backend
            pipeline_result = fn(pq.query, **kwargs)
            ingested = (pipeline_result.get("meta") or {}).get("ingested", False)
            coverage = (pipeline_result.get("meta") or {}).get("coverage", 0)
            result["searched"].append(
                {
                    "query": pq.query,
                    "topic": pq.topic,
                    "reason": pq.reason,
                    "ingested": ingested,
                    "coverage": coverage,
                }
            )
            write_audit(
                cycle_id=cycle_id,
                phase="proactive",
                action="proactive_search",
                target=pq.query,
                outcome="ingested" if ingested else "no_new_content",
                details={"topic": pq.topic, "coverage": coverage},
                mode=mode,
                data_path=data_path,
            )
        except Exception as e:
            log.warning("governance.phase4: proactive search error: %s", e)
            result["searched"].append(
                {"query": pq.query, "topic": pq.topic, "reason": pq.reason, "error": str(e)[:200]}
            )


def _build_async_items(async_queries: list, retryable_jobs: list, data_path: str, cycle_id: str) -> list[dict]:
    async_items: list[dict] = []
    for pq in async_queries:
        trace_id = f"gov_trace_{uuid.uuid4().hex[:10]}"
        write_trace_event(
            data_path,
            trace_id,
            TRACE_QUEUED,
            query=pq.query,
            topic=pq.topic,
            reason=pq.reason,
            job_type="proactive",
            cycle_id=cycle_id,
        )
        async_items.append({"trace_id": trace_id, "query": pq.query, "topic": pq.topic, "job_type": "proactive"})
    for job in retryable_jobs:
        query = job.get("query", "")
        if not query:
            continue
        trace_id = f"gov_trace_{uuid.uuid4().hex[:10]}"
        write_trace_event(
            data_path,
            trace_id,
            TRACE_QUEUED,
            query=query,
            job_id=job.get("job_id"),
            job_type="replay",
            cycle_id=cycle_id,
        )
        async_items.append({"trace_id": trace_id, "query": query, "job_id": job.get("job_id"), "job_type": "replay"})
    return async_items


def _start_async_governance_thread(
    async_items: list[dict],
    data_path: str,
    cycle_id: str,
    mode: str,
    fn: Callable,
    sync_queries: list,
    async_queries: list,
    retryable_jobs: list,
    result: dict[str, Any],
    backend: Any = None,
) -> None:
    if not async_items:
        return
    write_audit(
        cycle_id=cycle_id,
        phase="proactive",
        action="async_queue",
        outcome=f"queued_{len(async_items)}",
        details={"proactive": len(async_queries), "replays": len(retryable_jobs)},
        mode=mode,
        data_path=data_path,
    )
    thread = threading.Thread(
        target=_run_async_governance_batch,
        args=(async_items, data_path, cycle_id, mode, fn, backend),
        daemon=True,
        name=f"gov-async-{cycle_id[:20]}",
    )
    thread.start()
    result["_async_thread"] = thread
    log.info("governance.phase4: sync=%d async=%d (thread=%s)", len(sync_queries), len(async_items), thread.name)


def phase4_proactive(
    data_path: str,
    cycle_id: str,
    mode: str,
    interests: list,
    retryable_jobs: list,
    dry_run: bool,
    run_fn: Callable | None,
    backend: Any = None,
    max_proactive: int = 5,
    use_llm_queries: bool = False,
) -> dict:
    """Phase 4: Proactive search -- hybrid sync + async."""
    from .interest_analyzer import generate_proactive_queries

    result: dict[str, Any] = {"searched": [], "replayed": [], "async_queued": 0, "skipped_dry_run": False}
    if dry_run:
        result["skipped_dry_run"] = True
        write_audit(
            cycle_id=cycle_id,
            phase="proactive",
            action="skip_dry_run",
            outcome="skipped",
            mode=mode,
            data_path=data_path,
        )
        return result

    fn = _resolve_run_fn(run_fn)
    sync_budget = _get_sync_budget()
    existing = _load_existing_queries(data_path)
    queries = generate_proactive_queries(
        interests, existing_queries=existing, max_queries=max_proactive, use_llm=use_llm_queries
    )
    sync_queries = queries[:sync_budget]
    async_queries = queries[sync_budget:]

    _run_sync_proactive_queries(sync_queries, fn, result, cycle_id, mode, data_path, backend=backend)
    async_items = _build_async_items(async_queries, retryable_jobs, data_path, cycle_id)
    result["async_queued"] = len(async_items)
    result["_async_thread"] = None
    _start_async_governance_thread(
        async_items,
        data_path,
        cycle_id,
        mode,
        fn,
        sync_queries,
        async_queries,
        retryable_jobs,
        result,
        backend=backend,
    )
    return result


def phase5_report(
    data_path: str,
    cycle_id: str,
    mode: str,
    collect_data: dict,
    audit_data: dict,
    flags: list[dict],
    proactive_data: dict,
    harvest_data: list[dict] | None = None,
) -> dict:
    """Phase 5: Generate governance report data structure."""
    # Count pending review items
    pending_path = os.path.join(data_path, "pending_review.jsonl")
    pending_count = 0
    if os.path.exists(pending_path):
        with open(pending_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status", "pending") == "pending":
                        pending_count += 1
                except json.JSONDecodeError:
                    continue

    freshness = audit_data.get("freshness", {})
    total_resources = (
        len(freshness.get("fresh", [])) + len(freshness.get("aging", [])) + len(freshness.get("stale", []))
    )

    # Compute health score (0-100)
    if total_resources > 0:
        fresh_ratio = len(freshness.get("fresh", [])) / total_resources
        stale_ratio = len(freshness.get("stale", [])) / total_resources
        health_score = int(fresh_ratio * 80 + (1 - stale_ratio) * 20)
    else:
        health_score = -1  # unknown

    report: dict[str, Any] = {
        "cycle_id": cycle_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "overview": {
            "total_resources": total_resources,
            "health_score": health_score,
        },
        "knowledge_health": {
            "fresh": len(freshness.get("fresh", [])),
            "aging": len(freshness.get("aging", [])),
            "stale": len(freshness.get("stale", [])),
            "coverage_mean": collect_data.get("query_metrics", {}).get("coverage", {}).get("mean", 0),
        },
        "flags": {
            "total": len(flags),
            "by_type": {},
        },
        "proactive": {
            "queries_run": len(proactive_data.get("searched", [])),
            "ingested": sum(1 for s in proactive_data.get("searched", []) if s.get("ingested")),
            "async_queued": proactive_data.get("async_queued", 0),
            "dry_run": proactive_data.get("skipped_dry_run", False),
        },
        "async_harvest": {
            "harvested": len(harvest_data or []),
            "ingested": sum(1 for h in (harvest_data or []) if (h.get("result") or {}).get("ingested")),
        },
        "pending_review_count": pending_count,
        "weak_topics": [
            {"topic": t.get("topic", ""), "avg_coverage": t.get("avg_coverage", 0)}
            for t in collect_data.get("weak_topics", [])[:10]
        ],
    }

    # Flags by type
    by_type: dict[str, int] = {}
    for flag in flags:
        ft = flag.get("flag_type", "unknown")
        by_type[ft] = by_type.get(ft, 0) + 1
    flags_section: dict = report["flags"]  # type: ignore[assignment]
    flags_section["by_type"] = by_type

    # Load current pending flags for report embedding (top-N by severity)
    from .config import GOVERNANCE_REPORT_TOP_FLAGS

    _sev_order = {"high": 0, "medium": 1, "low": 2}
    all_pending = load_flags(data_path=data_path, status="pending")
    all_pending.sort(key=lambda f: _sev_order.get(f.get("severity", "low"), 2))
    top_flags = all_pending[:GOVERNANCE_REPORT_TOP_FLAGS]
    report["pending_flags"] = [
        {
            "flag_id": f.get("flag_id", ""),
            "flag_type": f.get("flag_type", ""),
            "severity": f.get("severity", ""),
            "uri": f.get("uri", ""),
            "reason": f.get("reason", ""),
        }
        for f in top_flags
    ]
    report["pending_flags_total"] = len(all_pending)

    # Team mode extras
    if mode == "team":
        report["query_metrics"] = collect_data.get("query_metrics", {})
        report["ttl_suggestions"] = audit_data.get("ttl_suggestions", [])
        report["audit_log"] = load_audit_log(data_path=data_path, cycle_id=cycle_id)
        report["config_snapshot"] = {
            "governance_enabled": env("CURATOR_GOVERNANCE_ENABLED", "0"),
            "governance_interval_hours": env("CURATOR_GOVERNANCE_INTERVAL_HOURS", "168"),
            "governance_mode": env("CURATOR_GOVERNANCE_MODE", "normal"),
            "governance_max_proactive": env("CURATOR_GOVERNANCE_MAX_PROACTIVE", "5"),
            "governance_lookback_days": env("CURATOR_GOVERNANCE_LOOKBACK_DAYS", "30"),
            "governance_sync_budget": env("CURATOR_GOVERNANCE_SYNC_BUDGET", "0"),
            "cov_sufficient": env("CURATOR_THRESHOLD_COV_SUFFICIENT", "0.55"),
        }

    write_audit(
        cycle_id=cycle_id,
        phase="report",
        action="generate_report",
        outcome="ok",
        mode=mode,
        data_path=data_path,
    )

    return report
