"""Pipeline v2: 返回结构化数据，不生成回答。

通过 KnowledgeBackend 接口与知识库交互，默认使用 OpenViking 后端。
可替换为 Milvus / Qdrant / Chroma / pgvector 等任何实现了 KnowledgeBackend 的后端。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .config import DATA_PATH, MAX_L2_DEPTH, OPENVIKING_CONFIG_FILE, log, validate_config
from .decision_report import format_report, format_report_short
from .memory_capture import capture_case
from .metrics import Metrics
from .retrieval_v2 import assess_coverage, load_context, ov_retrieve
from .review import detect_conflict, judge_and_ingest
from .router import route_scope
from .search import cross_validate, external_search
from .session_manager import OVClient, SessionManager

if TYPE_CHECKING:
    from .backend import KnowledgeBackend

# Serializes concurrent async ingest operations to prevent overlapping writes.
_ingest_lock = threading.Lock()


def _init_backend():
    """Initialize the knowledge backend. Uses OV by default.

    Returns:
        An :class:`OpenVikingBackend` instance.
    """
    from .backend_ov import OpenVikingBackend

    return OpenVikingBackend()


def _init_session_manager() -> tuple:
    """初始化 OV 客户端和 session manager。自动选嵌入/HTTP模式。

    Returns:
        Tuple of ``(OVClient, SessionManager)``.
    """
    ov = OVClient()  # 根据 OV_BASE_URL env 自动选模式

    if not ov.health():
        raise RuntimeError(f"OV 不可用 (mode={ov.mode})")

    sid_file = os.path.join(DATA_PATH, ".curator_session_id")
    sm = SessionManager(ov, sid_file)
    return ov, sm


def _do_judge_ingest(
    backend,
    query,
    context_text,
    external_txt,
    scope,
    used_uris,
    auto_ingest,
    m,
    trace,
    *,
    async_mode: bool = False,
):
    """Execute cross_validate → judge → ingest. Shared by sync and async paths.

    When called from the async path, *m* and *trace* are ``None`` (metrics
    are not recorded for background work — the pipeline already returned).
    Set *async_mode* to ``True`` so that ingest failures are persisted to
    ``async_ingest_failures.jsonl`` for observability.

    Returns dict with ``cv_warnings``, ``conflict``, ``ingested`` keys.
    """
    cv_warnings = []
    conflict = {"has_conflict": False, "summary": "", "points": []}
    ingested = False

    # B3: cross_validate 只在 need_fresh 时跑
    if scope.get("need_fresh"):
        cv = cross_validate(query, external_txt, scope)
        external_txt = cv.get("validated", external_txt)
        cv_warnings = cv.get("warnings", [])
        if trace is not None:
            trace["llm_calls"] += 1
        if m is not None:
            m.step("cross_validate", True, {"warnings": len(cv_warnings)})
    else:
        if m is not None:
            m.step("cross_validate", False, {"reason": "skipped_not_fresh"})

    # B2: judge + conflict 合并为一次 LLM 调用
    judge_result = judge_and_ingest(
        backend,
        query,
        context_text,
        external_txt,
        cv_warnings=cv_warnings,
    )
    if trace is not None:
        trace["llm_calls"] += 1
    if m is not None:
        m.step(
            "judge_and_conflict",
            True,
            {
                "pass": judge_result.get("pass"),
                "trust": judge_result.get("trust"),
                "has_conflict": judge_result.get("has_conflict"),
            },
        )

    # Gather local feedback signals for bidirectional conflict resolution
    local_signals = _aggregate_local_signals(used_uris)

    conflict = {
        "has_conflict": judge_result.get("has_conflict", False),
        "summary": judge_result.get("conflict_summary", ""),
        "points": judge_result.get("conflict_points", []),
        "resolution": _resolve_conflict(judge_result, local_signals=local_signals),
    }

    if judge_result.get("pass") and judge_result.get("markdown"):
        freshness = judge_result.get("freshness", "unknown")
        if freshness != "outdated":
            conflict_preferred = conflict.get("resolution", {}).get("preferred", "none")
            if conflict_preferred in ("human_review", "local"):
                if m is not None:
                    m.step(
                        "ingest",
                        False,
                        {
                            "reason": f"conflict_blocked:{conflict_preferred}",
                            "conflict_summary": conflict.get("summary", ""),
                        },
                    )
                log.info("冲突阻止入库: preferred=%s, summary=%s", conflict_preferred, conflict.get("summary", ""))
                _write_pending(
                    query,
                    judge_result,
                    conflict,
                    reason=f"conflict:{conflict_preferred}",
                    source_urls=_extract_urls(external_txt),
                )
            elif auto_ingest:
                try:
                    from .review import ingest_markdown_v2

                    ing = ingest_markdown_v2(
                        backend,
                        query[:60],
                        judge_result["markdown"],
                        freshness=freshness,
                        source_urls=_extract_urls(external_txt),
                        quality_feedback={
                            "judge_trust": judge_result.get("trust", 0),
                            "judge_reason": judge_result.get("reason", ""),
                            "has_conflict": judge_result.get("has_conflict", False),
                            "conflict_summary": judge_result.get("conflict_summary", ""),
                        },
                        uri_hints=list(used_uris),
                    )
                    ingested = True
                    if m is not None:
                        m.step("ingest", True, {"uri": ing.get("root_uri", "")})
                        _verify_ingest(backend, query, ing.get("root_uri", ""), m)
                    log.info("已入库: %s", ing.get("root_uri", ""))
                except Exception as e:
                    log.warning("ingest failed: %s", e)
                    if async_mode:
                        _log_async_failure(query, e)
                    if m is not None:
                        m.step("ingest", False, {"error": str(e)})
            else:
                if m is not None:
                    m.step("ingest", False, {"reason": "review_mode_pending"})
                log.info("审核模式: 内容待人工确认，未自动入库")
                _write_pending(
                    query,
                    judge_result,
                    conflict,
                    reason="review_mode",
                    source_urls=_extract_urls(external_txt),
                )

    return {"cv_warnings": cv_warnings, "conflict": conflict, "ingested": ingested, "external_text": external_txt}


def run(query: str, client=None, auto_ingest: bool = True, backend: KnowledgeBackend = None) -> dict:
    """Main pipeline v2 — 返回结构化数据，调用方自己组装 LLM 上下文。

    Args:
        query: User query string.
        client: Deprecated. Use ``backend`` instead.
        auto_ingest: Whether to automatically ingest passing external results.
        backend: Optional :class:`KnowledgeBackend`. If ``None``, initialises
                 the default OpenViking backend.

    Returns:
        Dict with keys: ``query``, ``ov_results``, ``context_text``,
        ``external_text``, ``coverage``, ``conflict``, ``meta``, ``metrics``,
        ``case_path``.

    LLM 调用策略（省 token）：
    - 覆盖率足够 → 0 次 LLM
    - 外搜（普通） → 1 次（judge+conflict 合并）
    - 外搜（需验证时效） → 2 次（+cross_validate）
    """
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    result = {
        "query": query,
        "ov_results": {},
        "context_text": "",
        "external_text": "",
        "coverage": 0.0,
        "conflict": {},
        "meta": {},
        "metrics": {},
        "case_path": None,
        "decision_report": "",
    }

    # ── decision_trace: 记录关键决策路径 ──
    trace = {
        "load_stage": "none",
        "llm_calls": 0,
        "external_reason": "not_evaluated",
    }

    # ── Step 1: 初始化 + 路由 ──
    log.info("STEP 1/4 初始化 + 路由...")

    # Init backend (used for ingest / verify)
    if backend is None:
        backend = _init_backend()

    try:
        ov, sm = _init_session_manager()
    except Exception as e:
        log.error("OV 初始化失败: %s", e)
        result["meta"]["error"] = f"知识库服务不可用: {e}"
        result["decision_report"] = format_report(result)
        return result

    m.step("init", True)

    scope = route_scope(query)
    m.step("route", True, {"domain": scope.get("domain")})
    log.info("STEP 1 完成: domain=%s", scope.get("domain"))

    sm.add_user_query(query)

    # ── Step 2: OV 检索 ──
    log.info("STEP 2/4 OV 检索...")
    retrieval_result = ov_retrieve(sm, query, limit=10)
    all_items = retrieval_result["all_items"]
    m.step(
        "retrieve",
        True,
        {
            "memories": len(retrieval_result["memories"]),
            "resources": len(retrieval_result["resources"]),
            "skills": len(retrieval_result["skills"]),
            "total": len(all_items),
        },
    )

    result["ov_results"] = retrieval_result

    # ── Step 3: 分层加载 + 覆盖率评估 ──
    log.info("STEP 3/4 加载内容...")
    context_text, used_uris, load_stage = load_context(backend, all_items, query, max_l2=MAX_L2_DEPTH)
    coverage, need_external, cov_reason = assess_coverage(retrieval_result, query=query)
    m.step("load_context", True, {"coverage": coverage, "used_uris": len(used_uris), "reason": cov_reason})
    m.score("coverage_before_external", round(coverage, 3))
    log.info(
        "STEP 3 完成: coverage=%.2f, reason=%s, stage=%s, used=%d", coverage, cov_reason, load_stage, len(used_uris)
    )

    trace["load_stage"] = load_stage
    trace["external_reason"] = cov_reason

    # ── 自动记录 OV 命中（feedback 学习）──
    # 被 load_context 实际采用的 URI 记一次 adopt，
    # 供下次检索时 rerank_with_feedback() 微调排名。
    if used_uris:
        try:
            from curator import feedback_store

            for uri in used_uris:
                feedback_store.apply(uri, "adopt")
            log.debug("feedback adopt: %d uris", len(used_uris))
        except Exception as _fb_err:
            log.debug("feedback_store 不可用，跳过: %s", _fb_err)

    result["context_text"] = context_text
    result["coverage"] = coverage

    # ── Step 4: 外搜 + 审核 + 冲突（可选，合并 LLM 调用）──
    external_txt = ""
    ingested = False
    cv_warnings = []
    conflict = {"has_conflict": False, "summary": "", "points": []}
    async_ingest_pending = False

    if need_external:
        m.flag("external_triggered", True)
        m.flag("external_reason", cov_reason)
        log.info("STEP 4/4 外部搜索... reason=%s", cov_reason)
        try:
            external_txt = external_search(query, scope)
            m.step("external_search", True, {"len": len(external_txt), "reason": cov_reason})
            log.info("STEP 4a 外搜完成: %d chars", len(external_txt))
        except Exception as e:
            log.warning("STEP 4 外搜失败: %s", e)
            m.step("external_search", False, {"error": str(e)})

        if external_txt:
            # Async ingest: when enabled + auto_ingest, defer judge+ingest
            # to a background thread so the user gets results faster.
            # Read env var dynamically so tests can override via patch.dict
            use_async = (os.environ.get("CURATOR_ASYNC_INGEST", "0") == "1") and auto_ingest

            if use_async:
                async_ingest_pending = True

                from .async_jobs import create_job, update_job

                _job_id = create_job(query, scope=scope)

                def _bg_judge_ingest(_jid=_job_id):
                    update_job(_jid, "running")
                    with _ingest_lock:
                        try:
                            _do_judge_ingest(
                                backend,
                                query,
                                context_text,
                                external_txt,
                                scope,
                                used_uris,
                                auto_ingest,
                                None,
                                None,
                                async_mode=True,
                            )
                            update_job(_jid, "success")
                        except Exception as e:
                            log.warning("async judge+ingest failed: %s", e)
                            _log_async_failure(query, e)
                            update_job(_jid, "failed", error=str(e))

                threading.Thread(target=_bg_judge_ingest, daemon=True).start()
                log.info("async ingest: job %s deferred to background thread", _job_id)
                m.step("judge_and_conflict", False, {"reason": "async_deferred", "job_id": _job_id})
            else:
                # Synchronous path (default) — no lock needed because the
                # caller blocks until completion; concurrent sync runs each
                # have their own backend/query context and don't share state.
                judge_out = _do_judge_ingest(
                    backend,
                    query,
                    context_text,
                    external_txt,
                    scope,
                    used_uris,
                    auto_ingest,
                    m,
                    trace,
                )
                cv_warnings = judge_out.get("cv_warnings", cv_warnings)
                conflict = judge_out.get("conflict", conflict)
                ingested = judge_out.get("ingested", False)
                external_txt = judge_out.get("external_text", external_txt)
    else:
        # B1: 不触发外搜 → 跳过冲突检测（0 次 LLM）
        m.flag("external_triggered", False)
        m.flag("external_reason", cov_reason)
        log.info("STEP 4/4 跳过外搜+冲突检测: %s", cov_reason)

    m.flag("has_conflict", conflict.get("has_conflict", False))
    result["external_text"] = external_txt
    result["conflict"] = conflict

    # ── Session 反馈 ──
    summary = f"检索完成: coverage={coverage:.2f}, sources={len(used_uris)}, external={'是' if need_external else '否'}"
    sm.add_assistant_response(summary, used_uris)
    sm.maybe_commit()
    m.step("feedback", True)

    # ── 结果组装 ──
    report = m.finalize()

    case_path = None
    if os.getenv("CURATOR_CAPTURE_CASE", "1") in ("1", "true"):
        case_path = capture_case(query, scope, report, context_text, out_dir=os.getenv("CURATOR_CASE_DIR", "cases"))

    result["meta"] = {
        "coverage": coverage,
        "coverage_reason": cov_reason,
        "external_triggered": report["flags"].get("external_triggered", False),
        "external_reason": cov_reason,
        "has_conflict": conflict.get("has_conflict", False),
        "ingested": ingested,
        "async_ingest_pending": async_ingest_pending,
        "used_uris": used_uris,
        "warnings": cv_warnings,
        "memories_count": len(retrieval_result["memories"]),
        "resources_count": len(retrieval_result["resources"]),
        "skills_count": len(retrieval_result["skills"]),
        "decision_trace": trace,  # D1
    }
    result["metrics"] = {
        "duration_sec": report["duration_sec"],
        "flags": report["flags"],
        "scores": report["scores"],
    }
    result["case_path"] = case_path
    result["decision_report"] = format_report(result)

    log.info(
        "完成: %.1fs, coverage=%.2f, external=%s, llm_calls=%d",
        report["duration_sec"],
        coverage,
        report["flags"].get("external_triggered"),
        trace["llm_calls"],
    )

    # 写 query 日志
    _log_query(
        query,
        coverage,
        need_external,
        cov_reason,
        used_uris,
        trace,
        ingested=ingested,
        async_ingest_pending=async_ingest_pending,
        need_fresh=scope.get("need_fresh", False),
        has_conflict=conflict.get("has_conflict", False),
        external_len=len(external_txt),
        auto_ingest=auto_ingest,
    )

    return result


def _extract_urls(text: str) -> list[str]:
    """Extract unique URLs from text, preserving order."""
    if not text:
        return []
    raw = re.findall(r"https?://[^\s)\]>\"']+", text)
    out: list[str] = []
    seen = set()
    for u in raw:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _log_async_failure(query: str, error: Exception) -> None:
    """Persist async ingest failures to DATA_PATH/async_ingest_failures.jsonl.

    This gives operators visibility into background judge+ingest failures
    that would otherwise be silently swallowed.
    """
    try:
        os.makedirs(DATA_PATH, exist_ok=True)
        log_path = os.path.join(DATA_PATH, "async_ingest_failures.jsonl")
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
        log_dir = DATA_PATH
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
    pending_path = os.path.join(DATA_PATH, "pending_review.jsonl")
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


def _verify_ingest(backend: KnowledgeBackend, query: str, new_uri: str, m: Metrics):
    """C1: 入库后轻量验证 — 检查新 URI 是否出现在检索结果中。

    Args:
        backend: Knowledge backend to search against.
        query: Original user query.
        new_uri: URI of the newly ingested resource.
        m: Metrics collector.
    """
    if not new_uri:
        return
    try:
        resp = backend.find(query, limit=5)
        found_uris = [r.uri for r in resp.results]
        hit = any(new_uri in u for u in found_uris)
        m.step("ingest_verify", True, {"hit": hit, "new_uri": new_uri})
        if hit:
            log.info("入库验证通过: %s", new_uri)
        else:
            log.debug("入库验证未命中（OV 索引尚未就绪，属正常现象）: %s", new_uri)
    except Exception as e:
        m.step("ingest_verify", False, {"error": str(e)})


def _aggregate_local_signals(used_uris: list | set) -> dict | None:
    """Aggregate feedback signals for local URIs used in this run.

    Returns dict with adopt_count, up_count, down_count summed across
    all used URIs. Returns None if feedback_store is unavailable.
    """
    if not used_uris:
        return None
    try:
        from curator import feedback_store

        data = feedback_store.load()
        adopt = up = down = 0
        for uri in used_uris:
            item = data.get(uri, {})
            adopt += item.get("adopt", 0)
            up += item.get("up", 0)
            down += item.get("down", 0)
        return {"adopt_count": adopt, "up_count": up, "down_count": down}
    except Exception as e:
        log.debug("failed to load feedback signals for URIs %s: %s", used_uris, e)
        return None


def _resolve_conflict(judge_result: dict, *, local_signals: dict | None = None) -> dict:
    """Conflict resolution strategy — bidirectional scoring.

    Scores both external and local knowledge to decide which to prefer.
    External score is based on judge trust + freshness.
    Local score is based on feedback signals (adopt/up/down).

    When neither side is clearly stronger, defers to human review.

    Args:
        judge_result: Output from judge_and_ingest (has trust, freshness, etc.)
        local_signals: Optional dict with ``adopt_count``, ``up_count``,
            ``down_count`` from feedback_store. ``None`` means no data.

    Returns:
        Dict with ``strategy``, ``preferred``, ``reason``, and ``scores``.
    """
    no_conflict = {
        "strategy": "no_conflict",
        "preferred": "none",
        "reason": "",
        "scores": {"external": 0, "local": 0},
    }
    if not judge_result.get("has_conflict"):
        return no_conflict

    trust = judge_result.get("trust", 5)
    freshness = judge_result.get("freshness", "unknown")

    strategy = os.environ.get("CURATOR_CONFLICT_STRATEGY", "auto")

    if strategy == "local":
        return {
            "strategy": "local_always",
            "preferred": "local",
            "reason": "config: always prefer local",
            "scores": {"external": 0, "local": 0},
        }
    elif strategy == "external":
        return {
            "strategy": "external_always",
            "preferred": "external",
            "reason": "config: always prefer external",
            "scores": {"external": 0, "local": 0},
        }
    elif strategy == "human":
        return {
            "strategy": "human_always",
            "preferred": "human_review",
            "reason": "config: always human review",
            "scores": {"external": 0, "local": 0},
        }

    # ── Score external source ──
    # trust: 0-10 from judge LLM
    # freshness bonus: current=+2, recent=+1, stale=-1, outdated=-2
    freshness_bonus = {"current": 2, "recent": 1, "unknown": 0, "stale": -2, "outdated": -3}
    ext_score = trust + freshness_bonus.get(freshness, 0)

    # ── Score local knowledge ──
    # Based on feedback signals: adopt is strongest (used by pipeline),
    # up/down are explicit user feedback
    local_score = 5.0  # neutral baseline
    if local_signals is not None:
        adopt = local_signals.get("adopt_count", 0)
        up = local_signals.get("up_count", 0)
        down = local_signals.get("down_count", 0)
        # adopt is weighted higher (objective signal from pipeline)
        local_score = 5.0 + min(adopt * 0.3, 3.0) + min(up * 0.5, 2.0) - min(down * 0.7, 3.0)
        local_score = max(0, min(12, local_score))
    else:
        # No feedback data → local score stays at neutral
        local_score = 5.0

    scores = {"external": round(ext_score, 2), "local": round(local_score, 2)}

    # ── Decision ──
    margin = 2.0  # minimum gap to make a confident decision
    diff = ext_score - local_score

    if diff >= margin:
        preferred = "external"
        reason = f"external stronger (ext={ext_score:.1f} vs local={local_score:.1f}, diff={diff:+.1f})"
    elif diff <= -margin:
        preferred = "local"
        reason = f"local stronger (local={local_score:.1f} vs ext={ext_score:.1f}, diff={diff:+.1f})"
    else:
        preferred = "human_review"
        reason = (
            f"scores too close (ext={ext_score:.1f} vs local={local_score:.1f}, diff={diff:+.1f}), needs human judgment"
        )

    return {"strategy": "auto", "preferred": preferred, "reason": reason, "scores": scores}
