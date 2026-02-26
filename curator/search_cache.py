"""Search result cache — avoids redundant external API calls for recent queries.

Cache is a simple JSON file under DATA_PATH with LRU eviction.  Disabled by
default (``CURATOR_CACHE_ENABLED=0``).  Two TTL tiers:

- ``CACHE_TTL`` (default 3600s): normal queries
- ``CACHE_FRESH_TTL`` (default 300s): queries with ``need_fresh`` scope flag

Thread safety is guaranteed via ``file_lock.locked_rw_json``.
"""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata

from .config import CACHE_ENABLED, CACHE_FRESH_TTL, CACHE_MAX_ENTRIES, CACHE_TTL, DATA_PATH, log


def _cache_path() -> str:
    return os.path.join(DATA_PATH, "search_cache.json")


def _normalize(text: str) -> str:
    """Normalize query text for cache key: NFKC + lowercase + strip."""
    return unicodedata.normalize("NFKC", text).strip().lower()


def _cache_key(query: str, domain: str) -> str:
    """SHA-256 based cache key (first 16 hex chars)."""
    raw = f"{_normalize(query)}|{domain}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def get(query: str, scope: dict) -> str | None:
    """Look up cached search result.  Returns ``None`` on miss or expiry."""
    if not CACHE_ENABLED:
        return None

    from .file_lock import locked_rw_json

    domain = scope.get("domain", "")
    key = _cache_key(query, domain)
    need_fresh = scope.get("need_fresh", False)
    ttl = CACHE_FRESH_TTL if need_fresh else CACHE_TTL
    now = time.time()

    def _lookup(data: dict):
        entry = data.get(key)
        if entry is None:
            return None
        ts = entry.get("ts", 0)
        if now - ts > ttl:
            # expired — remove entry
            data.pop(key, None)
            return None
        # update access time for LRU (but keep original ts for TTL)
        entry["last_access"] = now
        return entry.get("text")

    try:
        return locked_rw_json(_cache_path(), _lookup)
    except Exception as e:
        log.debug("search_cache get error: %s", e)
        return None


def put(query: str, scope: dict, text: str) -> None:
    """Store a search result in cache.  Skips empty text."""
    if not CACHE_ENABLED:
        return
    if not text or not text.strip():
        return

    from .file_lock import locked_rw_json

    domain = scope.get("domain", "")
    key = _cache_key(query, domain)
    now = time.time()

    def _store(data: dict):
        data[key] = {
            "query": query,
            "domain": domain,
            "text": text,
            "ts": now,
            "last_access": now,
        }
        # LRU eviction: remove oldest entries if over max
        if len(data) > CACHE_MAX_ENTRIES:
            sorted_keys = sorted(data.keys(), key=lambda k: data[k].get("last_access", 0))
            excess = len(data) - CACHE_MAX_ENTRIES
            for k in sorted_keys[:excess]:
                data.pop(k, None)

    try:
        locked_rw_json(_cache_path(), _store)
    except Exception as e:
        log.debug("search_cache put error: %s", e)


def clear() -> None:
    """Remove all cached entries."""
    path = _cache_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            log.debug("search_cache clear error: %s", e)


def stats() -> dict:
    """Return cache statistics: total entries + file path."""
    from .file_lock import locked_rw_json

    path = _cache_path()
    if not os.path.exists(path):
        return {"entries": 0, "path": path}

    try:
        count = locked_rw_json(path, lambda d: len(d))
        return {"entries": count, "path": path}
    except Exception:
        return {"entries": 0, "path": path}
