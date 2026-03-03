"""Governance traces — async trace event lifecycle.

Traces track background governance tasks (proactive searches, retryable
replays).  Each task transitions through: queued -> done / failed -> consumed.

Events are appended to ``governance_async_traces.jsonl`` using sidecar
file-locking (``file_lock.locked_append``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .config import DATA_PATH

# ── Constants ─────────────────────────────────────────────────────────────────

ASYNC_TRACE_FILE = "governance_async_traces.jsonl"

TRACE_QUEUED = "queued"
TRACE_DONE = "done"
TRACE_FAILED = "failed"
TRACE_CONSUMED = "consumed"

# Max age for orphaned "queued" traces before they're considered abandoned
_TRACE_ORPHAN_HOURS = 48


# ── Helpers ───────────────────────────────────────────────────────────────────


def _traces_path(data_path: str) -> str:
    return os.path.join(data_path, ASYNC_TRACE_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────────────────────


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
