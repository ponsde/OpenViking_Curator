"""Governance audit log — append-only structured audit trail.

Every governance phase writes audit entries to ``governance_log.jsonl``
via ``write_audit()``.  Entries are loaded/filtered by ``load_audit_log()``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .config import DATA_PATH

# ── Constants ─────────────────────────────────────────────────────────────────

AUDIT_FILE = "governance_log.jsonl"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _audit_path(data_path: str) -> str:
    return os.path.join(data_path, AUDIT_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────────────────────


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
