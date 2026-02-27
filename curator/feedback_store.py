#!/usr/bin/env python3
import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decay_factor(last_decay_iso: str, half_life_days: float) -> float:
    """Compute exponential decay factor since *last_decay_iso* timestamp."""
    try:
        last = datetime.fromisoformat(last_decay_iso)
        delta_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
        return math.pow(0.5, delta_days / half_life_days)
    except Exception:
        return 1.0  # safe fallback — no decay applied


def _ensure_stats_v2(rec: dict) -> dict:
    """Initialise stats_v2 from legacy counters if missing (migration helper)."""
    if "stats_v2" not in rec:
        up = float(rec.get("up", 0))
        down = float(rec.get("down", 0))
        adopt = float(rec.get("adopt", 0))
        seen = up + down + adopt
        rec["stats_v2"] = {
            "up_w": up,
            "down_w": down,
            "adopt_w": adopt,
            "seen_w": max(seen, 1.0),
            "last_decay_at": _now_iso(),
            "last_event_at": _now_iso(),
            "schema_version": _SCHEMA_VERSION,
        }
    return rec


def _apply_decay_to_stats(stats: dict, half_life_days: float) -> None:
    """Apply decay in-place to stats_v2 dict (lazy decay pattern)."""
    factor = _decay_factor(stats.get("last_decay_at", _now_iso()), half_life_days)
    if factor >= 0.9999:
        return  # negligible decay, skip write
    for key in ("up_w", "down_w", "adopt_w", "seen_w"):
        stats[key] = max(0.0, stats.get(key, 0.0) * factor)
    stats["last_decay_at"] = _now_iso()


try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows fallback

STORE = Path(os.getenv("CURATOR_FEEDBACK_FILE", "./feedback.json"))


def _resolve_store() -> Path:
    """Return the active feedback store path.

    Prefers the CURATOR_FEEDBACK_FILE env var (re-read each call so that
    monkeypatch.setenv and runtime overrides take effect).  Falls back to the
    module-level STORE, which may itself be monkey-patched in unit tests.
    """
    env_path = os.getenv("CURATOR_FEEDBACK_FILE")
    return Path(env_path) if env_path else STORE


def _locked_rw(fn):
    """Read-modify-write with exclusive file lock (Unix) or no-lock fallback (Windows)."""
    store = _resolve_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.touch(exist_ok=True)
    with open(store, "r+", encoding="utf-8") as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read().strip()
            data = json.loads(raw) if raw else {}
            result = fn(data)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            return result
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)


def load(store_path: str | os.PathLike | None = None):
    store = Path(store_path) if store_path else _resolve_store()
    if store.exists():
        with open(store, "r", encoding="utf-8") as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw = f.read().strip()
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                log.warning("feedback store corrupted, returning empty: %s", store)
                return {}
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_UN)
    return {}


def save(data):
    store = _resolve_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "w", encoding="utf-8") as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)


def apply(uri: str, action: str):
    def _update(data):
        if action not in ("up", "down", "adopt"):
            raise ValueError("action must be one of: up, down, adopt")
        item = data.get(uri, {"up": 0, "down": 0, "adopt": 0})
        # IMPORTANT: increment the legacy counter BEFORE calling _ensure_stats_v2.
        # _ensure_stats_v2 seeds stats_v2 from the current counter values, so the
        # current event must already be reflected in item[action] at migration time.
        # Changing this order would cause the first event to be missing from stats_v2.
        item[action] = item.get(action, 0) + 1

        # Update time-decayed stats_v2 when enabled
        from .config import FEEDBACK_DECAY_ENABLED, FEEDBACK_HALF_LIFE_DAYS

        if FEEDBACK_DECAY_ENABLED:
            stats_v2_existed = "stats_v2" in item
            _ensure_stats_v2(item)  # creates from legacy counters if missing
            _apply_decay_to_stats(item["stats_v2"], FEEDBACK_HALF_LIFE_DAYS)
            if stats_v2_existed:
                # Existing stats_v2: apply this event on top of decayed weights
                item["stats_v2"][f"{action}_w"] = item["stats_v2"].get(f"{action}_w", 0.0) + 1.0
                item["stats_v2"]["seen_w"] = item["stats_v2"].get("seen_w", 1.0) + 1.0
            # When freshly created: migration already reflects the current increment
            item["stats_v2"]["last_event_at"] = _now_iso()

        data[uri] = item
        return item

    return _locked_rw(_update)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Curator feedback store")
    p.add_argument("uri", help="resource uri")
    p.add_argument("action", choices=["up", "down", "adopt"])
    args = p.parse_args()
    s = apply(args.uri, args.action)
    print("ok", args.uri, s)
