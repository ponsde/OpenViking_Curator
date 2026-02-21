"""Dedup: resources 去重（只报告模式）。

OV 的 memory_deduplicator 只管 memory 去重，不管 resources。
这个模块补充 resources 层的相似度检测。

注意：只报告疑似重复，不自动删除/合并。

All knowledge-store operations go through KnowledgeBackend (or duck-typed compatible).
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from .config import log

if TYPE_CHECKING:
    from .backend import KnowledgeBackend

DEDUP_LOG_FILE = os.getenv(
    "CURATOR_DEDUP_LOG",
    os.path.join(
        os.environ.get("CURATOR_DATA_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")),
        "dedup_log.json",
    ),
)
SIMILARITY_THRESHOLD = 0.55


def _load_dedup_log() -> dict:
    try:
        if os.path.exists(DEDUP_LOG_FILE):
            return json.loads(Path(DEDUP_LOG_FILE).read_text())
    except Exception:
        pass
    return {"checked_pairs": [], "reports": [], "last_run": None}


def _save_dedup_log(state: dict):
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["checked_pairs"] = state["checked_pairs"][-500:]
    state["reports"] = state["reports"][-100:]

    log_path = Path(DEDUP_LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a = a[:1000].lower().strip()
    b = b[:1000].lower().strip()
    return SequenceMatcher(None, a, b).ratio()


def _pair_key(uri_a: str, uri_b: str) -> str:
    return "|".join(sorted([uri_a, uri_b]))


def scan_duplicates(backend, uris: list[str], max_checks: int = 5) -> dict:
    """扫描 resources 中的疑似重复（只报告，不删除）。

    Args:
        backend: A :class:`KnowledgeBackend` instance (or any object with a
                 ``read(uri)`` method).
        uris: List of resource URIs to compare.
        max_checks: Maximum number of pair-wise comparisons.

    Returns:
        Dict with ``checked`` (int) and ``duplicates`` (list of dicts with
        ``uri_a``, ``uri_b``, ``similarity``).
    """
    state = _load_dedup_log()
    checked_set = set(state["checked_pairs"])

    result = {"checked": 0, "duplicates": []}

    valid_uris = [u for u in uris if u.startswith("viking://") or u.startswith("mem://")]
    if len(valid_uris) < 2:
        return result

    # 读取内容
    uri_contents = {}
    for u in valid_uris[:10]:
        try:
            content = str(backend.read(u))
            if content and len(content) > 50:
                uri_contents[u] = content
        except Exception:
            continue

    if len(uri_contents) < 2:
        return result

    checks_done = 0
    uri_list = list(uri_contents.keys())

    for i in range(len(uri_list)):
        if checks_done >= max_checks:
            break
        for j in range(i + 1, len(uri_list)):
            if checks_done >= max_checks:
                break

            uri_a, uri_b = uri_list[i], uri_list[j]
            pk = _pair_key(uri_a, uri_b)

            if pk in checked_set:
                continue

            sim = _text_similarity(uri_contents[uri_a], uri_contents[uri_b])
            checked_set.add(pk)
            state["checked_pairs"].append(pk)
            checks_done += 1
            result["checked"] += 1

            if sim >= SIMILARITY_THRESHOLD:
                log.info("dedup: 疑似重复 (%.2f): %s vs %s", sim, uri_a, uri_b)
                dup = {"uri_a": uri_a, "uri_b": uri_b, "similarity": round(sim, 3)}
                result["duplicates"].append(dup)
                state["reports"].append({
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **dup,
                })

    _save_dedup_log(state)
    return result
