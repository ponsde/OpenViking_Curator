"""Query logging and pending review persistence.

Extracted from pipeline_v2.py (D2 refactor). Internal module, not part of public API.

Functions read ``DATA_PATH`` from ``pipeline_v2`` at call time (late import)
so that test patches on ``curator.pipeline_v2.DATA_PATH`` propagate correctly.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from .config import log


def _get_data_path() -> str:
    """Return the current DATA_PATH from pipeline_v2 (supports test patching)."""
    from . import pipeline_v2 as _pv2  # noqa: F811 – late import, no circular at call time

    return _pv2.DATA_PATH


def _log_async_failure(query: str, error: Exception) -> None:
    """Persist async ingest failures to DATA_PATH/async_ingest_failures.jsonl.

    This gives operators visibility into background judge+ingest failures
    that would otherwise be silently swallowed.
    """
    try:
        data_path = _get_data_path()
        os.makedirs(data_path, exist_ok=True)
        log_path = os.path.join(data_path, "async_ingest_failures.jsonl")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "error": str(error),
            "error_type": type(error).__name__,
        }
        from .file_lock import locked_append

        locked_append(log_path, json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("failed to write async failure log: %s", e)


def _log_query(
    query: str,
    coverage: float,
    need_external: bool,
    reason: str,
    used_uris: list,
    trace: dict,
    *,
    ingested: bool = False,
    async_ingest_pending: bool = False,
    need_fresh: bool = False,
    has_conflict: bool = False,
    external_len: int = 0,
    auto_ingest: bool = True,
) -> None:
    """写 query 日志到 data/query_log.jsonl（append 模式，失败不影响主流程）。

    Schema v2: 增加 ingested/async/need_fresh/conflict/external_len/auto_ingest
    字段，供 query_log_aggregate.py 分析使用。
    """
    try:
        log_dir = _get_data_path()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "query_log.jsonl")
        entry = {
            "schema_version": 2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "coverage": round(coverage, 4),
            "external_triggered": bool(need_external),
            "reason": reason,
            "used_uris": list(used_uris) if used_uris else [],
            "load_stage": trace.get("load_stage", "unknown"),
            "llm_calls": trace.get("llm_calls", 0),
            "ingested": ingested,
            "async_ingest_pending": async_ingest_pending,
            "need_fresh": need_fresh,
            "has_conflict": has_conflict,
            "external_len": external_len,
            "auto_ingest": auto_ingest,
        }
        from .file_lock import locked_append

        locked_append(log_path, json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("query log 写入失败（不影响主流程）: %s", e)


def _write_pending(
    query: str, judge_result: dict, conflict: dict, reason: str, source_urls: list[str] | None = None
) -> None:
    """待审核内容持久化到 DATA_PATH/pending_review.jsonl。

    当内容通过 judge 但因冲突或审核模式未自动入库时调用。
    append-only，每行一个 JSON 对象，包含完整 markdown + 决策上下文。

    Args:
        query: Original user query.
        judge_result: Output from judge_and_ingest.
        conflict: Conflict resolution dict from pipeline.
        reason: Why ingest was blocked (e.g. 'conflict:human_review', 'review_mode').
        source_urls: Extracted source URLs from external_txt (for review_cli approve).
    """
    data_path = _get_data_path()
    pending_path = os.path.join(data_path, "pending_review.jsonl")
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "query": query,
        "trust": judge_result.get("trust", 0),
        "freshness": judge_result.get("freshness", "unknown"),
        "conflict_summary": conflict.get("summary", ""),
        "conflict_preferred": conflict.get("resolution", {}).get("preferred", ""),
        "source_urls": source_urls or [],
        "markdown": judge_result.get("markdown", ""),
    }
    try:
        from .file_lock import locked_append

        locked_append(pending_path, json.dumps(entry, ensure_ascii=False) + "\n")
        log.info("pending review 已写入: %s (reason=%s)", pending_path, reason)
    except Exception as e:
        log.warning("pending review 写入失败: %s", e)
