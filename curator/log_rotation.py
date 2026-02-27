"""JSONL log rotation: size-based with numbered backups.

When a .jsonl file exceeds ``max_mb`` megabytes, it is rotated:
  query_log.jsonl       → query_log.1.jsonl
  query_log.1.jsonl     → query_log.2.jsonl  (if exists)
  query_log.2.jsonl     → query_log.3.jsonl  (if exists)
  query_log.{keep}.jsonl → deleted

Rotation is performed *before* the next append so that the active file
stays below the limit.  Uses fcntl advisory locking on Unix to avoid
races with concurrent pipeline processes.
"""

from __future__ import annotations

import os

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


def _numbered_path(base: str, n: int) -> str:
    """Return the n-th backup path: ``foo.jsonl`` → ``foo.1.jsonl``."""
    root, ext = os.path.splitext(base)
    return f"{root}.{n}{ext}"


def maybe_rotate(path: str | os.PathLike, *, max_mb: float = 5.0, keep: int = 3, _locked: bool = False) -> bool:
    """Rotate *path* if it exceeds *max_mb*.

    Returns True if rotation occurred.  Safe to call on every append —
    the size check is a cheap stat() and only triggers rename when needed.

    Args:
        path: The .jsonl file to check.
        max_mb: Maximum file size in megabytes before rotating.
                0 disables rotation.
        keep: Number of backup files to keep (1–20).
        _locked: If True, skip acquiring the sidecar lock (caller already holds it).
    """
    if max_mb <= 0:
        return False

    path = str(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return False

    if size < max_mb * 1024 * 1024:
        return False

    return _do_rotate(path, max_mb=max_mb, keep=keep, _locked=_locked)


def _do_rotate(path: str, *, max_mb: float, keep: int, _locked: bool) -> bool:
    """Internal rotate with optional sidecar locking."""
    lock_fd = None
    try:
        if not _locked:
            lock_path = path + ".lock"
            os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
            lock_fd = open(lock_path, "w")  # noqa: SIM115
            if _HAS_FCNTL:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Re-check size under lock (another process may have rotated already)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size < max_mb * 1024 * 1024:
            return False

        # Shift backups: N → N+1, drop oldest
        for i in range(keep, 0, -1):
            if i == keep:
                oldest = _numbered_path(path, keep)
                if os.path.exists(oldest):
                    os.remove(oldest)
                continue
            dst = _numbered_path(path, i + 1)
            src = _numbered_path(path, i)
            if os.path.exists(src):
                os.rename(src, dst)

        # Rotate current → .1
        os.rename(path, _numbered_path(path, 1))
        return True
    finally:
        if lock_fd is not None:
            if _HAS_FCNTL:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
