"""Governance flags — create, load, update, expire, and batch operations.

Flags are advisory markers written to ``governance_flags.jsonl``.
The full lifecycle is: pending -> keep / delete / adjust / ignore / expired.

All writes use sidecar file-locking (``file_lock.locked_append`` /
``file_lock.locked_rw_jsonl``) for safe concurrency.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import DATA_PATH

# ── Constants ─────────────────────────────────────────────────────────────────

FLAG_FILE = "governance_flags.jsonl"

FLAG_TYPES = frozenset({"stale_resource", "broken_url", "review_expired", "ttl_rebalance"})
# Flag lifecycle: pending -> keep / delete / adjust / ignore / expired
FLAG_STATUSES = frozenset({"pending", "keep", "delete", "adjust", "ignore", "expired"})
SEVERITIES = frozenset({"low", "medium", "high"})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _flags_path(data_path: str) -> str:
    return os.path.join(data_path, FLAG_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────────────────────


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

    flag: dict[str, Any] = {
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


def load_flags(
    data_path: str | None = None,
    status: str | None = None,
    flag_type: str | None = None,
    severity: str | None = None,
    cycle_id: str | None = None,
) -> list[dict]:
    """Load governance flags, optionally filtered by status, flag_type, severity, cycle_id."""
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
                if status is not None and flag.get("status") != status:
                    continue
                if flag_type is not None and flag.get("flag_type") != flag_type:
                    continue
                if severity is not None and flag.get("severity") != severity:
                    continue
                if cycle_id is not None and flag.get("cycle_id") != cycle_id:
                    continue
                flags.append(flag)
            except json.JSONDecodeError:
                continue
    return flags


def update_flag_status(
    flag_id: str,
    new_status: str,
    data_path: str | None = None,
    reason: str | None = None,
) -> bool:
    """Update a flag's status in the JSONL file.  Returns True if found.

    Uses the same sidecar lock (``path + ".lock"``) as ``create_flag`` /
    ``locked_append`` so that concurrent flag writes and updates are
    mutually exclusive.

    Args:
        flag_id:    Full flag ID to update.
        new_status: New status value (must be in FLAG_STATUSES).
        data_path:  Override data directory (for testing).
        reason:     Optional decision reason recorded in ``resolution_reason``.
    """
    if new_status not in FLAG_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r} (expected one of {sorted(FLAG_STATUSES)})")

    from .file_lock import locked_rw_jsonl

    _data = data_path or DATA_PATH
    path = _flags_path(_data)
    if not os.path.exists(path):
        return False

    resolved_at = _now_iso() if new_status != "pending" else None

    def _update(items: list[dict]) -> bool:
        found = False
        for flag in items:
            if flag.get("flag_id") == flag_id:
                flag["status"] = new_status
                if resolved_at is not None:
                    flag["resolved_at"] = resolved_at
                flag["resolution_reason"] = reason
                found = True
        return found

    return locked_rw_jsonl(path, _update)


def expire_flags(
    data_path: str | None = None,
    expire_days: int = 90,
) -> list[str]:
    """Mark pending flags older than expire_days as 'expired'.

    Returns list of expired flag_ids.  expire_days=0 disables (no-op).
    """
    if expire_days <= 0:
        return []

    from .file_lock import locked_rw_jsonl

    _data = data_path or DATA_PATH
    path = _flags_path(_data)
    if not os.path.exists(path):
        return []

    now = datetime.now(timezone.utc)
    resolved_at = now.isoformat()

    def _expire(items: list[dict]) -> list[str]:
        expired_ids: list[str] = []
        for flag in items:
            if flag.get("status") == "pending":
                ts_str = flag.get("timestamp", "")
                if ts_str:
                    try:
                        created_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        age_days = (now - created_at).total_seconds() / 86400
                        if age_days > expire_days:
                            flag["status"] = "expired"
                            flag["resolved_at"] = resolved_at
                            flag["resolution_reason"] = f"auto-expired after {expire_days} days"
                            expired_ids.append(flag["flag_id"])
                    except (ValueError, TypeError):
                        pass
        return expired_ids

    return locked_rw_jsonl(path, _expire)


def batch_update_flags(
    flag_ids: list[str],
    new_status: str,
    reason: str | None = None,
    data_path: str | None = None,
) -> tuple[list[str], list[str]]:
    """Batch update multiple flags atomically in a single read-modify-write.

    Returns (updated_ids, not_found_ids).
    """
    if new_status not in FLAG_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r} (expected one of {sorted(FLAG_STATUSES)})")
    if not flag_ids:
        return [], []

    from .file_lock import locked_rw_jsonl

    _data = data_path or DATA_PATH
    path = _flags_path(_data)
    if not os.path.exists(path):
        return [], list(flag_ids)

    target_ids = set(flag_ids)
    resolved_at = _now_iso()

    def _batch_update(items: list[dict]) -> list[str]:
        updated_ids: list[str] = []
        for flag in items:
            if flag.get("flag_id") in target_ids:
                flag["status"] = new_status
                flag["resolved_at"] = resolved_at
                flag["resolution_reason"] = reason
                updated_ids.append(flag["flag_id"])
        return updated_ids

    updated_ids = locked_rw_jsonl(path, _batch_update)
    not_found = [fid for fid in flag_ids if fid not in set(updated_ids)]
    return updated_ids, not_found
