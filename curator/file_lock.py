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

    Automatically rotates the file if it exceeds the configured size limit
    (``CURATOR_LOG_ROTATE_MB``, default 5 MB).  Rotation and append share
    a single sidecar lock to prevent race conditions.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # Use sidecar lock to serialise rotate + append as one critical section
    lock_path = path + ".lock"
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        if _HAS_FCNTL:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Rotate if needed (under the same lock)
        try:
            from .config import LOG_ROTATE_KEEP, LOG_ROTATE_MB
            from .log_rotation import maybe_rotate

            maybe_rotate(path, max_mb=LOG_ROTATE_MB, keep=LOG_ROTATE_KEEP, _locked=True)
        except Exception:
            pass  # rotation failure must not block the append

        # Append under the same lock
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    finally:
        if _HAS_FCNTL:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


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


def locked_rw_jsonl(path: str | os.PathLike, fn):
    """Read-modify-write a JSONL file under an exclusive sidecar lock.

    *fn* receives a list of parsed dicts (one per JSONL line) and may mutate
    it in place.  The modified list is written back.  Returns whatever *fn*
    returns.  Uses the same sidecar ``.lock`` file as ``locked_append`` to
    ensure mutual exclusion.
    """
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    lock_path = path + ".lock"
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        if _HAS_FCNTL:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

        items: list[dict] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        try:
                            items.append(json.loads(stripped))
                        except json.JSONDecodeError:
                            pass

        result = fn(items)

        with open(path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        return result
    finally:
        if _HAS_FCNTL:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
