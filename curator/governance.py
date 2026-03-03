"""Governance: automated weekly knowledge maintenance cycle.

Orchestrates 6 phases:
0. Harvest async results from previous cycle (trace file)
1. Data collection (read-only, 0 side-effects)
2. Database audit (read backend, 0 modifications)
3. Soft flagging (write governance_flags.jsonl, no deletes)
4. Proactive search -- fully async by default (daemon thread)
5. Report generation

Phase 4 queues all proactive searches and retryable replays to a
background thread.  Results are written as trace events to
``governance_async_traces.jsonl`` and harvested by the next cycle
(Phase 0).  ``CURATOR_GOVERNANCE_SYNC_BUDGET`` (default 0) controls
how many queries run synchronously before the cycle returns.

All flags are advisory -- no auto-deletion.  User decides via CLI.

Implementation is split across sub-modules:
- governance_flags.py   : flag CRUD + constants
- governance_traces.py  : async trace lifecycle
- governance_audit.py   : audit log
- governance_phases.py  : phase 0-5 implementations

This module re-exports all public symbols so that existing
``from curator.governance import ...`` continues to work.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

# Re-export CURATED_DIR so tests that monkeypatch
# "curator.governance.CURATED_DIR" continue to work.
from .config import (
    CURATED_DIR,  # noqa: F401
    DATA_PATH,
    env,
    log,
)

# Re-export locked_append for callers that imported it via governance
from .file_lock import locked_append  # noqa: F401

# ── Re-exports (backward compatibility) ──────────────────────────────────────
# All public symbols are re-exported here so that:
#   from curator.governance import create_flag, load_flags, ...
# continues to work without changes.
from .governance_audit import (  # noqa: F401
    AUDIT_FILE,
    load_audit_log,
    write_audit,
)
from .governance_flags import (  # noqa: F401
    FLAG_FILE,
    FLAG_STATUSES,
    FLAG_TYPES,
    SEVERITIES,
    batch_update_flags,
    create_flag,
    expire_flags,
    load_flags,
    update_flag_status,
)
from .governance_phases import (  # noqa: F401
    _run_async_governance_batch,
)
from .governance_phases import (
    phase1_collect as _phase1_collect,
)
from .governance_phases import (
    phase2_audit as _phase2_audit,
)
from .governance_phases import (
    phase3_flag as _phase3_flag,
)
from .governance_phases import (
    phase4_proactive as _phase4_proactive,
)
from .governance_phases import (
    phase5_report as _phase5_report,
)
from .governance_traces import (  # noqa: F401
    ASYNC_TRACE_FILE,
    TRACE_CONSUMED,
    TRACE_DONE,
    TRACE_FAILED,
    TRACE_QUEUED,
    harvest_async_results,
    load_trace_states,
    write_trace_event,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _cycle_id() -> str:
    return f"gov_cycle_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


# ── Main entry point ─────────────────────────────────────────────────────────


def _phase0_harvest(_data: str, cid: str, _mode: str) -> list[dict]:
    try:
        harvest_data = harvest_async_results(_data, consumed_by=cid)
        if harvest_data:
            write_audit(
                cycle_id=cid,
                phase="harvest",
                action="harvest_async",
                outcome=f"harvested_{len(harvest_data)}",
                details={"ingested": sum(1 for h in harvest_data if (h.get("result") or {}).get("ingested"))},
                mode=_mode,
                data_path=_data,
            )
            log.info("governance.phase0: harvested %d async results", len(harvest_data))
        return harvest_data
    except Exception as e:
        log.warning("governance.phase0: harvest failed: %s", e, exc_info=True)
        return []


def _run_governance_phases(
    _data: str,
    cid: str,
    _mode: str,
    lookback: int,
    backend: Any,
    dry_run: bool,
    _run_fn: Callable | None,
    max_proactive: int,
    use_llm_q: bool,
) -> tuple[list[dict], dict, dict, list[dict], dict, dict]:
    harvest_data = _phase0_harvest(_data, cid, _mode)
    try:
        collect_data = _phase1_collect(_data, lookback, cid, _mode)
    except Exception as e:
        log.warning("governance.phase1: failed: %s", e, exc_info=True)
        collect_data = {"weak_topics": [], "query_metrics": {}, "interests": []}
    try:
        audit_data = _phase2_audit(_data, cid, _mode, backend)
    except Exception as e:
        log.warning("governance.phase2: failed: %s", e, exc_info=True)
        audit_data = {}
    try:
        flags = _phase3_flag(_data, cid, _mode, audit_data)
    except Exception as e:
        log.warning("governance.phase3: failed: %s", e, exc_info=True)
        flags = []
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
    try:
        report = _phase5_report(
            _data, cid, _mode, collect_data, audit_data, flags, proactive_data, harvest_data=harvest_data
        )
    except Exception as e:
        log.warning("governance.phase5: failed: %s", e, exc_info=True)
        report = {"cycle_id": cid, "mode": _mode, "error": str(e)}
    return harvest_data, collect_data, audit_data, flags, proactive_data, report


def _finalize_cycle(
    report: dict,
    _data: str,
    cid: str,
    _mode: str,
    t0: float,
    flags: list[dict],
    proactive_data: dict,
    harvest_data: list[dict],
) -> dict:
    report["duration_sec"] = round(time.time() - t0, 2)
    report_path = os.path.join(_data, f"governance_report_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json")
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
        "governance: cycle %s completed in %.1fs -- flags=%d sync=%d async_queued=%d harvested=%d",
        cid,
        report["duration_sec"],
        len(flags),
        len(proactive_data.get("searched", [])),
        proactive_data.get("async_queued", 0),
        len(harvest_data),
    )
    report["_async_thread"] = proactive_data.get("_async_thread")
    return report


def run_governance_cycle(
    backend: Any = None,
    *,
    data_path: str | None = None,
    mode: str = "normal",
    dry_run: bool = False,
    _run_fn: Callable | None = None,
) -> dict:
    """Execute a complete governance cycle."""
    _data = data_path or DATA_PATH
    _mode = mode if mode in ("normal", "team") else "normal"
    lookback = int(env("CURATOR_GOVERNANCE_LOOKBACK_DAYS", "30"))
    max_proactive = int(env("CURATOR_GOVERNANCE_MAX_PROACTIVE", "5"))
    use_llm_q = env("CURATOR_GOVERNANCE_USE_LLM_QUERIES", "").lower() in ("1", "true", "yes")
    os.makedirs(_data, exist_ok=True)
    cid = _cycle_id()
    log.info("governance: starting cycle %s (mode=%s, dry_run=%s)", cid, _mode, dry_run)
    t0 = time.time()
    write_audit(cycle_id=cid, phase="start", action="cycle_start", outcome="started", mode=_mode, data_path=_data)

    harvest_data, _collect_data, _audit_data, flags, proactive_data, report = _run_governance_phases(
        _data, cid, _mode, lookback, backend, dry_run, _run_fn, max_proactive, use_llm_q
    )
    return _finalize_cycle(report, _data, cid, _mode, t0, flags, proactive_data, harvest_data)
