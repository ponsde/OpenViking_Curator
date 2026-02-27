"""Governance: automated weekly knowledge maintenance cycle.

Orchestrates 6 phases:
0. Harvest async results from previous cycle (trace file)
1. Data collection (read-only, 0 side-effects)
2. Database audit (read backend, 0 modifications)
3. Soft flagging (write governance_flags.jsonl, no deletes)
4. Proactive search — fully async by default (daemon thread)
5. Report generation

Phase 4 queues all proactive searches and retryable replays to a
background thread.  Results are written as trace events to
``governance_async_traces.jsonl`` and harvested by the next cycle
(Phase 0).  ``CURATOR_GOVERNANCE_SYNC_BUDGET`` (default 0) controls
how many queries run synchronously before the cycle returns.

All flags are advisory — no auto-deletion.  User decides via CLI.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .config import CURATED_DIR, DATA_PATH, env, log

# ── Flag / Audit log constants ───────────────────────────────────────────────

FLAG_FILE = "governance_flags.jsonl"
AUDIT_FILE = "governance_log.jsonl"
ASYNC_TRACE_FILE = "governance_async_traces.jsonl"

FLAG_TYPES = frozenset({"stale_resource", "broken_url", "review_expired", "ttl_rebalance"})
# Flag lifecycle: pending → keep / delete / adjust / ignore
FLAG_STATUSES = frozenset({"pending", "keep", "delete", "adjust", "ignore"})
SEVERITIES = frozenset({"low", "medium", "high"})

# Async trace lifecycle: queued → done / failed → consumed
TRACE_QUEUED = "queued"
TRACE_DONE = "done"
TRACE_FAILED = "failed"
TRACE_CONSUMED = "consumed"

# Max age for orphaned "queued" traces before they're considered abandoned
_TRACE_ORPHAN_HOURS = 48


# ── Helpers ──────────────────────────────────────────────────────────────────


def _flags_path(data_path: str) -> str:
    return os.path.join(data_path, FLAG_FILE)


def _audit_path(data_path: str) -> str:
    return os.path.join(data_path, AUDIT_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_id() -> str:
    return f"gov_cycle_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def create_flag(
    *,
    cycle_id: str,
    uri: str,
    flag_type: str,
    severity: str,
    reason: str,
    details: dict | None = None,
    data_path: str | None = None,
) -> dict:
    """Create and persist a governance flag.  Returns the flag dict."""
    if flag_type not in FLAG_TYPES:
        raise ValueError(f"Invalid flag_type: {flag_type!r} (expected one of {sorted(FLAG_TYPES)})")
    if severity not in SEVERITIES:
        raise ValueError(f"Invalid severity: {severity!r} (expected one of {sorted(SEVERITIES)})")

    from .file_lock import locked_append

    flag = {
        "flag_id": f"gov_{uuid.uuid4().hex[:10]}",
        "timestamp": _now_iso(),
        "cycle_id": cycle_id,
        "uri": uri,
        "flag_type": flag_type,
        "severity": severity,
        "reason": reason,
        "details": details or {},
        "status": "pending",
    }
    _data = data_path or DATA_PATH
    locked_append(_flags_path(_data), json.dumps(flag, ensure_ascii=False) + "\n")
    return flag


def write_audit(
    *,
    cycle_id: str,
    phase: str,
    action: str,
    target: str = "",
    outcome: str = "",
    details: dict | None = None,
    mode: str = "normal",
    data_path: str | None = None,
) -> dict:
    """Append an audit log entry.  Returns the entry dict."""
    from .file_lock import locked_append

    entry = {
        "timestamp": _now_iso(),
        "cycle_id": cycle_id,
        "phase": phase,
        "action": action,
        "target": target,
        "outcome": outcome,
        "details": details or {},
        "mode": mode,
    }
    _data = data_path or DATA_PATH
    locked_append(_audit_path(_data), json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_flags(data_path: str | None = None, status: str | None = None) -> list[dict]:
    """Load governance flags, optionally filtered by status."""
    _data = data_path or DATA_PATH
    path = _flags_path(_data)
    if not os.path.exists(path):
        return []
    flags: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                flag = json.loads(line)
                if status is None or flag.get("status") == status:
                    flags.append(flag)
            except json.JSONDecodeError:
                continue
    return flags


def update_flag_status(
    flag_id: str,
    new_status: str,
    data_path: str | None = None,
) -> bool:
    """Update a flag's status in the JSONL file.  Returns True if found.

    Uses the same sidecar lock (``path + ".lock"``) as ``create_flag`` /
    ``locked_append`` so that concurrent flag writes and updates are
    mutually exclusive.
    """
    if new_status not in FLAG_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r} (expected one of {sorted(FLAG_STATUSES)})")

    from .file_lock import _HAS_FCNTL

    _data = data_path or DATA_PATH
    path = _flags_path(_data)
    if not os.path.exists(path):
        return False

    # Use sidecar lock — same domain as locked_append / create_flag
    lock_path = path + ".lock"
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        if _HAS_FCNTL:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX)

        lines: list[str] = []
        found = False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    flag = json.loads(stripped)
                    if flag.get("flag_id") == flag_id:
                        flag["status"] = new_status
                        found = True
                    lines.append(json.dumps(flag, ensure_ascii=False) + "\n")
                except json.JSONDecodeError:
                    lines.append(line)

        if found:
            with open(path, "w", encoding="utf-8") as f:
                f.write("".join(lines))
        return found
    finally:
        if _HAS_FCNTL:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _traces_path(data_path: str) -> str:
    return os.path.join(data_path, ASYNC_TRACE_FILE)


def write_trace_event(
    data_path: str,
    trace_id: str,
    event: str,
    **extra,
) -> dict:
    """Append a trace event to the async traces file.  Returns the entry."""
    from .file_lock import locked_append

    entry = {
        "timestamp": _now_iso(),
        "trace_id": trace_id,
        "event": event,
        **extra,
    }
    locked_append(_traces_path(data_path), json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_trace_states(data_path: str | None = None) -> dict[str, dict]:
    """Build current state for each trace from events (latest event wins)."""
    _data = data_path or DATA_PATH
    path = _traces_path(_data)
    if not os.path.exists(path):
        return {}
    traces: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = entry.get("trace_id", "")
            if not tid:
                continue
            if tid not in traces:
                traces[tid] = {}
            # Merge fields from each event, update status
            traces[tid].update(entry)
            traces[tid]["status"] = entry.get("event", "unknown")
    return traces


def harvest_async_results(
    data_path: str,
    consumed_by: str,
) -> list[dict]:
    """Harvest completed async traces, mark as consumed.

    Also marks orphaned traces (queued > _TRACE_ORPHAN_HOURS) as failed.
    Returns list of completed trace dicts.
    """
    traces = load_trace_states(data_path)
    now = datetime.now(timezone.utc)
    harvested: list[dict] = []

    for tid, trace in traces.items():
        status = trace.get("status")

        if status == TRACE_DONE:
            harvested.append(trace)
            write_trace_event(data_path, tid, TRACE_CONSUMED, consumed_by=consumed_by)

        elif status == TRACE_QUEUED:
            # Check for orphaned traces
            ts_str = trace.get("timestamp", "")
            if ts_str:
                try:
                    queued_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age_hours = (now - queued_at).total_seconds() / 3600
                    if age_hours > _TRACE_ORPHAN_HOURS:
                        write_trace_event(
                            data_path,
                            tid,
                            TRACE_FAILED,
                            error=f"orphaned: queued {age_hours:.0f}h ago, never completed",
                        )
                except (ValueError, TypeError):
                    pass

    return harvested


def load_audit_log(data_path: str | None = None, cycle_id: str | None = None) -> list[dict]:
    """Load audit log entries, optionally filtered by cycle_id."""
    _data = data_path or DATA_PATH
    path = _audit_path(_data)
    if not os.path.exists(path):
        return []
    entries: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if cycle_id is None or entry.get("cycle_id") == cycle_id:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


# ── Phase implementations ────────────────────────────────────────────────────


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


def _phase1_collect(
    data_path: str,
    lookback_days: int,
    cycle_id: str,
    mode: str,
) -> dict:
    """Phase 1: Data collection — read query log, feedback, weak topics."""
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


def _freshness_scan_backend(backend: Any) -> dict[str, list[dict]]:
    """Backend-agnostic freshness scan using curator.freshness.

    Returns {fresh: [...], aging: [...], stale: [...]}.
    Each item: {uri, score, category}.
    """
    from .freshness import uri_freshness_score

    FRESH_THRESHOLD = 0.8
    AGING_THRESHOLD = 0.4

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
    """TTL rebalance scan using curator.usage_ttl + feedback_store.

    Scans curated markdown backups and computes TTL adjustment suggestions.
    Returns list of suggestion dicts for changed items.
    """
    import re

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

        # Skip files with no feedback data — absence of feedback ≠ unused.
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


def _phase2_audit(
    data_path: str,
    cycle_id: str,
    mode: str,
    backend: Any = None,
) -> dict:
    """Phase 2: Database audit — freshness scan, TTL rebalance, retryable jobs.

    Uses backend-agnostic freshness scoring via curator.freshness.
    When no backend is available (test/offline), returns empty audit data.
    """
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


def _phase3_flag(
    data_path: str,
    cycle_id: str,
    mode: str,
    audit_data: dict,
) -> list[dict]:
    """Phase 3: Soft flagging — create flags for issues found in audit.

    Deduplicates against existing pending flags (same URI + flag_type).
    """
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
    """Run a batch of governance searches in a background thread.

    Each item writes trace events on completion/failure.
    Items: [{"trace_id", "query", "topic"?, "job_type": "proactive"|"replay"}]
    """
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


def _phase4_proactive(
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
    """Phase 4: Proactive search — hybrid sync + async.

    Sync budget (CURATOR_GOVERNANCE_SYNC_BUDGET, default 3) limits how many
    proactive queries run synchronously for immediate feedback.  Remaining
    proactive queries and ALL retryable replays run in a background thread.
    Each async item writes trace events so the next cycle can harvest results.
    """
    from .interest_analyzer import generate_proactive_queries

    result: dict[str, Any] = {
        "searched": [],
        "replayed": [],
        "async_queued": 0,
        "skipped_dry_run": False,
    }

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

    # Resolve pipeline run function
    if run_fn is None:
        from .pipeline_v2 import run as _pipeline_run

        _fn: Callable = _pipeline_run
    else:
        _fn = run_fn

    try:
        sync_budget = max(0, int(env("CURATOR_GOVERNANCE_SYNC_BUDGET", "0")))
    except (ValueError, TypeError):
        sync_budget = 0

    # Generate proactive queries from interests
    existing: set[str] = set()
    ql_path = os.path.join(data_path, "query_log.jsonl")
    if os.path.exists(ql_path):
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

    queries = generate_proactive_queries(
        interests,
        existing_queries=existing,
        max_queries=max_proactive,
        use_llm=use_llm_queries,
    )

    # Split proactive queries: sync (budget) + async (rest)
    sync_queries = queries[:sync_budget]
    async_queries = queries[sync_budget:]

    # ── Sync: run budget-limited proactive searches ──
    for pq in sync_queries:
        try:
            kwargs: dict[str, Any] = {"auto_ingest": True}
            if backend is not None:
                kwargs["backend"] = backend
            pipeline_result = _fn(pq.query, **kwargs)
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
                {
                    "query": pq.query,
                    "topic": pq.topic,
                    "reason": pq.reason,
                    "error": str(e)[:200],
                }
            )

    # ── Async: queue remaining proactive + ALL retryable replays ──
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
        async_items.append(
            {
                "trace_id": trace_id,
                "query": pq.query,
                "topic": pq.topic,
                "job_type": "proactive",
            }
        )

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
        async_items.append(
            {
                "trace_id": trace_id,
                "query": query,
                "job_id": job.get("job_id"),
                "job_type": "replay",
            }
        )

    result["async_queued"] = len(async_items)
    result["_async_thread"] = None  # will be set if thread is launched

    if async_items:
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
            args=(async_items, data_path, cycle_id, mode, _fn, backend),
            daemon=True,
            name=f"gov-async-{cycle_id[:20]}",
        )
        thread.start()
        result["_async_thread"] = thread
        log.info(
            "governance.phase4: sync=%d async=%d (thread=%s)",
            len(sync_queries),
            len(async_items),
            thread.name,
        )

    return result


def _phase5_report(
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

    report = {
        "cycle_id": cycle_id,
        "timestamp": _now_iso(),
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


# ── Main entry point ─────────────────────────────────────────────────────────


def run_governance_cycle(
    backend: Any = None,
    *,
    data_path: str | None = None,
    mode: str = "normal",
    dry_run: bool = False,
    _run_fn: Callable | None = None,
) -> dict:
    """Execute a complete governance cycle.

    Args:
        backend:    KnowledgeBackend instance (optional, for freshness scan).
        data_path:  Override data directory (for testing).
        mode:       "normal" or "team" (team adds full audit trail).
        dry_run:    If True, skip Phase 4 (proactive search / writes).
        _run_fn:    Override pipeline run function (for testing).

    Returns:
        Governance report dict.
    """
    _data = data_path or DATA_PATH
    _mode = mode if mode in ("normal", "team") else "normal"
    lookback = int(env("CURATOR_GOVERNANCE_LOOKBACK_DAYS", "30"))
    max_proactive = int(env("CURATOR_GOVERNANCE_MAX_PROACTIVE", "5"))
    use_llm_q = env("CURATOR_GOVERNANCE_USE_LLM_QUERIES", "").lower() in ("1", "true", "yes")

    os.makedirs(_data, exist_ok=True)
    cid = _cycle_id()

    log.info("governance: starting cycle %s (mode=%s, dry_run=%s)", cid, _mode, dry_run)
    t0 = time.time()

    write_audit(
        cycle_id=cid,
        phase="start",
        action="cycle_start",
        outcome="started",
        mode=_mode,
        data_path=_data,
    )

    # Phase 0: Harvest async results from previous cycle(s)
    try:
        harvest_data = harvest_async_results(_data, consumed_by=cid)
        if harvest_data:
            write_audit(
                cycle_id=cid,
                phase="harvest",
                action="harvest_async",
                outcome=f"harvested_{len(harvest_data)}",
                details={
                    "ingested": sum(1 for h in harvest_data if (h.get("result") or {}).get("ingested")),
                },
                mode=_mode,
                data_path=_data,
            )
            log.info("governance.phase0: harvested %d async results", len(harvest_data))
    except Exception as e:
        log.warning("governance.phase0: harvest failed: %s", e, exc_info=True)
        harvest_data = []

    # Phase 1: Collect
    try:
        collect_data = _phase1_collect(_data, lookback, cid, _mode)
    except Exception as e:
        log.warning("governance.phase1: failed: %s", e, exc_info=True)
        collect_data = {"weak_topics": [], "query_metrics": {}, "interests": []}

    # Phase 2: Audit
    try:
        audit_data = _phase2_audit(_data, cid, _mode, backend)
    except Exception as e:
        log.warning("governance.phase2: failed: %s", e, exc_info=True)
        audit_data = {}

    # Phase 3: Flag
    try:
        flags = _phase3_flag(_data, cid, _mode, audit_data)
    except Exception as e:
        log.warning("governance.phase3: failed: %s", e, exc_info=True)
        flags = []

    # Phase 4: Proactive (hybrid sync + async)
    try:
        proactive_data = _phase4_proactive(
            _data,
            cid,
            _mode,
            interests=collect_data["interests"],
            retryable_jobs=audit_data.get("retryable_jobs", []),
            dry_run=dry_run,
            run_fn=_run_fn,
            backend=backend,
            max_proactive=max_proactive,
            use_llm_queries=use_llm_q,
        )
    except Exception as e:
        log.warning("governance.phase4: failed: %s", e, exc_info=True)
        proactive_data = {"searched": [], "async_queued": 0, "skipped_dry_run": False}

    # Phase 5: Report
    try:
        report = _phase5_report(
            _data,
            cid,
            _mode,
            collect_data,
            audit_data,
            flags,
            proactive_data,
            harvest_data=harvest_data,
        )
    except Exception as e:
        log.warning("governance.phase5: failed: %s", e, exc_info=True)
        report = {"cycle_id": cid, "mode": _mode, "error": str(e)}
    report["duration_sec"] = round(time.time() - t0, 2)

    # Save report to file (after duration_sec is set for consistency)
    report_path = os.path.join(
        _data,
        f"governance_report_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json",
    )
    from .file_lock import locked_write

    locked_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    log.info("governance: report saved to %s", report_path)

    write_audit(
        cycle_id=cid,
        phase="end",
        action="cycle_end",
        outcome="completed",
        details={
            "duration_sec": report["duration_sec"],
            "flags": len(flags),
            "async_queued": proactive_data.get("async_queued", 0),
            "harvested": len(harvest_data),
        },
        mode=_mode,
        data_path=_data,
    )

    log.info(
        "governance: cycle %s completed in %.1fs — flags=%d sync=%d async_queued=%d harvested=%d",
        cid,
        report["duration_sec"],
        len(flags),
        len(proactive_data.get("searched", [])),
        proactive_data.get("async_queued", 0),
        len(harvest_data),
    )

    # Attach thread reference for callers that need to wait (e.g. CLI)
    report["_async_thread"] = proactive_data.get("_async_thread")

    return report
