"""Shared file-locking utilities for concurrent-safe writes.

Provides ``locked_append`` (for JSONL appends) and ``locked_write`` (for
full-file overwrites) using ``fcntl.flock`` on Unix.  On Windows (no fcntl)
the lock is silently skipped — acceptable because Windows deployments are
not expected to face concurrent pipeline runs.

All locking is advisory (Unix convention).  Callers that use these helpers
consistently will be protected from interleaved writes.
"""

import json
import os

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows fallback


def locked_append(path: str | os.PathLike, line: str) -> None:
    """Append *line* to *path* under an exclusive file lock.

    Creates parent directories if needed.  The trailing newline is the
    caller's responsibility (most callers pass ``json.dumps(...) + "\\n"``).
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)


def locked_write(path: str | os.PathLike, content: str) -> None:
    """Overwrite *path* atomically under an exclusive file lock.

    Creates parent directories if needed.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if _HAS_FCNTL:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(content)
        finally:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)


def locked_rw_json(path: str | os.PathLike, fn):
    """Read-modify-write a JSON file under an exclusive lock.

    *fn* receives the parsed dict (or ``{}`` if the file is empty/missing)
    and may mutate it.  The modified dict is written back.  Returns whatever
    *fn* returns.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Ensure the file exists before opening r+
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
    with open(path, "r+", encoding="utf-8") as f:
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
