"""Pipeline v2: 返回结构化数据，不生成回答。

通过 KnowledgeBackend 接口与知识库交互，默认使用 OpenViking 后端。
可替换为 Milvus / Qdrant / Chroma / pgvector 等任何实现了 KnowledgeBackend 的后端。
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
import warnings
from typing import TYPE_CHECKING, Any

try:
    import structlog as _structlog

    def _bind_run_context(query: str, session_id: str | None) -> str:
        """Bind per-request context to structlog contextvars. Returns run_id."""
        run_id = uuid.uuid4().hex[:8]
        ctx: dict[str, str] = {"run_id": run_id, "query_prefix": query[:40]}
        if session_id:
            ctx["session_id"] = session_id
        _structlog.contextvars.bind_contextvars(**ctx)
        return run_id

    def _clear_run_context() -> None:
        _structlog.contextvars.clear_contextvars()

except ImportError:

    def _bind_run_context(query: str, session_id: str | None) -> str:  # type: ignore[misc]
        return uuid.uuid4().hex[:8]

    def _clear_run_context() -> None:  # type: ignore[misc]
        pass


from .config import (
    ADOPT_MIN_SCORE,
    ASYNC_INGEST,
    CAPTURE_CASE,
    CASE_DIR,
    DATA_PATH,
    MAX_L2_DEPTH,
    RETRIEVE_LIMIT,
    log,
    validate_config,
)
from .conflict_resolution import _aggregate_local_signals, _resolve_conflict
from .decision_report import format_report
from .memory_capture import capture_case
from .metrics import Metrics
from .query_log import _log_async_failure, _log_query, _write_pending
from .retrieval_v2 import assess_coverage, backend_retrieve, load_context
from .review import judge_and_ingest
from .router import route_scope
from .search import cross_validate, external_search

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


def _resolve_judge_conflict(
    judge_result: dict[str, Any], used_uris: list[str], feedback_data: dict | None
) -> dict[str, Any]:
    """Build conflict payload from judge output + local feedback signals."""
    local_signals = _aggregate_local_signals(used_uris, feedback_data=feedback_data)
    return {
        "has_conflict": judge_result.get("has_conflict", False),
        "summary": judge_result.get("conflict_summary", ""),
        "points": judge_result.get("conflict_points", []),
        "resolution": _resolve_conflict(judge_result, local_signals=local_signals),
    }


def _attempt_ingest(
    backend,
    query,
    judge_result,
    external_txt,
    used_uris,
    m,
    *,
    async_mode: bool = False,
) -> bool:
    """Execute ingest with common logging/metrics and async failure tracking."""
    from .review import ingest_markdown_v2

    freshness = judge_result.get("freshness", "unknown")
    try:
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
        if m is not None:
            m.step("ingest", True, {"uri": ing.get("root_uri", "")})
            _verify_ingest(backend, query, ing.get("root_uri", ""), m)
        log.info("已入库: %s", ing.get("root_uri", ""))
        return True
    except Exception as e:
        log.warning("ingest failed: %s", e)
        if async_mode:
            _log_async_failure(query, e)
        if m is not None:
            m.step("ingest", False, {"error": str(e)})
        return False


def _cross_validate_step(query, external_txt, scope, m, trace):
    """Run cross-validation when scope requires freshness check."""
    if scope.get("need_fresh"):
        cv = cross_validate(query, external_txt, scope)
        external_txt = cv.get("validated", external_txt)
        cv_warnings = cv.get("warnings", [])
        if trace is not None:
            trace["llm_calls"] += 1
        if m is not None:
            m.step("cross_validate", True, {"warnings": len(cv_warnings)})
        return external_txt, cv_warnings
    if m is not None:
        m.step("cross_validate", False, {"reason": "skipped_not_fresh"})
    return external_txt, []


def _decide_ingest(
    judge_result,
    conflict,
    query,
    external_txt,
    used_uris,
    backend,
    auto_ingest,
    m,
    async_mode,
):
    """Decide whether to ingest, block on conflict, or defer to review."""
    if not (judge_result.get("pass") and judge_result.get("markdown")):
        return False
    if judge_result.get("freshness", "unknown") == "outdated":
        return False
    preferred = conflict.get("resolution", {}).get("preferred", "none")
    if preferred in ("human_review", "local"):
        if m is not None:
            m.step(
                "ingest",
                False,
                {"reason": f"conflict_blocked:{preferred}", "conflict_summary": conflict.get("summary", "")},
            )
        log.info("冲突阻止入库: preferred=%s, summary=%s", preferred, conflict.get("summary", ""))
        _write_pending(
            query, judge_result, conflict, reason=f"conflict:{preferred}", source_urls=_extract_urls(external_txt)
        )
        return False
    if auto_ingest:
        return _attempt_ingest(backend, query, judge_result, external_txt, used_uris, m, async_mode=async_mode)
    if m is not None:
        m.step("ingest", False, {"reason": "review_mode_pending"})
    log.info("审核模式: 内容待人工确认，未自动入库")
    _write_pending(query, judge_result, conflict, reason="review_mode", source_urls=_extract_urls(external_txt))
    return False


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
    feedback_data: dict | None = None,
):
    """Execute cross_validate → judge → ingest. Shared by sync and async paths."""
    external_txt, cv_warnings = _cross_validate_step(query, external_txt, scope, m, trace)

    judge_result = judge_and_ingest(backend, query, context_text, external_txt, cv_warnings=cv_warnings)
    judge_degraded = judge_result.get("judge_degraded", False)
    if trace is not None:
        trace["llm_calls"] += 1
    if m is not None:
        jr = judge_result
        m.step(
            "judge_and_conflict",
            True,
            {"pass": jr.get("pass"), "trust": jr.get("trust"), "has_conflict": jr.get("has_conflict")},
        )

    conflict = _resolve_judge_conflict(judge_result, used_uris, feedback_data)
    ingested = _decide_ingest(
        judge_result,
        conflict,
        query,
        external_txt,
        used_uris,
        backend,
        auto_ingest,
        m,
        async_mode,
    )
    return {
        "cv_warnings": cv_warnings,
        "conflict": conflict,
        "ingested": ingested,
        "external_text": external_txt,
        "judge_degraded": judge_degraded,
        "async_ingest_pending": False,
    }


class CuratorPipeline:
    """Reusable pipeline instance — initialise backend + session once.

    Usage::

        pipeline = CuratorPipeline()          # one-time init
        r1 = pipeline.run("how to deploy?")   # reuses backend/session
        r2 = pipeline.run("what is RAG?")

    Health checks are cached for ``health_ttl`` seconds (default 60).
    """

    def __init__(
        self,
        backend: KnowledgeBackend | None = None,
        *,
        health_ttl: float = 60.0,
    ):
        validate_config()

        self._backend = backend if backend is not None else _init_backend()
        self._session_id: str | None = None
        self._health_ttl = health_ttl
        self._last_health_check: float = 0.0

        from .scheduler import start_scheduler

        start_scheduler()

    def _ensure_session(self) -> str | None:
        """Lazy-init backend session, with TTL health check.

        Returns:
            Session ID string, or ``None`` if sessions are not supported.
        """
        if not self._backend.supports_sessions:
            return None

        now = time.time()
        if self._session_id is not None and (now - self._last_health_check < self._health_ttl):
            return self._session_id

        if not self._backend.health():
            raise RuntimeError(f"Backend {self._backend.name} 不可用")

        if self._session_id is None:
            sid_file = os.path.join(DATA_PATH, ".curator_session_id")
            self._session_id = self._backend.load_or_create_session(sid_file)

        self._last_health_check = now
        return self._session_id

    @property
    def backend(self) -> KnowledgeBackend:
        return self._backend

    def run(self, query: str, *, auto_ingest: bool = True) -> dict:
        """Run the pipeline for *query*. See module-level ``run()`` for docs."""
        session_id = self._ensure_session()
        # _ensure_session already performed the health check; skip it in _run_impl
        return _run_impl(query, self._backend, auto_ingest, session_id, _skip_health=True)


def run(query: str, client=None, auto_ingest: bool = True, backend: KnowledgeBackend | None = None) -> dict:
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
    validate_config()

    if client is not None:
        warnings.warn(
            "pipeline_v2.run(client=...) is deprecated and will be removed in a future version; use backend=... instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    if backend is None:
        backend = _init_backend()

    # Module-level run() creates a fresh session per call (stateless)
    session_id = backend.create_session() if backend.supports_sessions else None

    return _run_impl(query, backend, auto_ingest, session_id)


def _run_impl(
    query: str,
    backend: KnowledgeBackend,
    auto_ingest: bool,
    session_id: str | None,
    *,
    _skip_health: bool = False,
) -> dict:
    """Shared implementation for CuratorPipeline.run() and module-level run()."""
    run_id = _bind_run_context(query, session_id)
    try:
        return _run_impl_inner(query, backend, auto_ingest, session_id, _skip_health, run_id)
    finally:
        _clear_run_context()


def _route_query(
    query: str,
    backend: KnowledgeBackend,
    session_id: str | None,
    _skip_health: bool,
    m: Metrics,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Step 1: init backend check + route scope, returns scope or early result."""
    log.info("STEP 1/4 初始化 + 路由...")

    if not _skip_health:
        try:
            if not backend.health():
                raise RuntimeError(f"Backend {backend.name} 不可用")
        except Exception as e:
            log.error("Backend 初始化失败: %s", e)
            result["meta"]["error"] = f"知识库服务不可用: {e}"
            result["meta"]["degraded"] = True
            result["meta"]["degraded_reasons"] = [f"backend: {backend.name} unavailable"]
            result["decision_report"] = format_report(result)
            return None

    m.step("init", True)
    scope = route_scope(query)
    m.step("route", True, {"domain": scope.get("domain")})
    log.info("STEP 1 完成: domain=%s", scope.get("domain"))

    if session_id and backend.supports_sessions:
        backend.session_add_message(session_id, "user", query)

    return scope


def _record_feedback_adopt(used_uris: list[str], all_items: list[dict]) -> None:
    """Record adopt feedback for used URIs with meaningful scores."""
    from . import feedback_store

    try:
        uri_scores = {it.get("uri", ""): it.get("score", 0) for it in all_items}
        adopted = 0
        for rank, uri in enumerate(used_uris):
            score = uri_scores.get(uri, 0)
            if score < ADOPT_MIN_SCORE:
                continue
            for _ in range(2 if rank == 0 else 1):
                feedback_store.apply(uri, "adopt")
            adopted += 1
        log.debug("feedback adopt: %d/%d uris (min_score=%.2f)", adopted, len(used_uris), ADOPT_MIN_SCORE)
    except Exception as _fb_err:
        log.debug("feedback_store 不可用，跳过: %s", _fb_err)


def _retrieve_context(
    backend: KnowledgeBackend,
    query: str,
    session_id: str | None,
    m: Metrics,
    result: dict[str, Any],
    trace: dict[str, Any],
    feedback_data: dict | None,
) -> dict[str, Any]:
    """Step 2+3: retrieve (L0/L1/L2), load context, assess coverage."""
    log.info("STEP 2/4 检索...")
    retrieval_result = backend_retrieve(
        backend,
        query,
        session_id=session_id,
        limit=RETRIEVE_LIMIT,
        feedback_data=feedback_data,
    )
    all_items = retrieval_result["all_items"]
    counts = {k: len(retrieval_result[k]) for k in ("memories", "resources", "skills")}
    counts["total"] = len(all_items)
    m.step("retrieve", True, counts)
    result["ov_results"] = retrieval_result

    log.info("STEP 3/4 加载内容...")
    context_text, used_uris, load_stage = load_context(backend, all_items, query, max_l2=MAX_L2_DEPTH)
    coverage, need_external, cov_reason = assess_coverage(retrieval_result, query=query)
    if not context_text.strip() and not need_external:
        log.info("load_context returned empty content despite coverage=%.2f; forcing external search", coverage)
        need_external, coverage, cov_reason = True, 0.0, "empty_content"

    m.step("load_context", True, {"coverage": coverage, "used_uris": len(used_uris), "reason": cov_reason})
    m.score("coverage_before_external", round(coverage, 3))
    log.info("STEP 3 完成: cov=%.2f reason=%s stage=%s used=%d", coverage, cov_reason, load_stage, len(used_uris))
    trace["load_stage"], trace["external_reason"] = load_stage, cov_reason

    if used_uris:
        _record_feedback_adopt(used_uris, all_items)

    result["context_text"], result["coverage"] = context_text, coverage
    return {
        "retrieval_result": retrieval_result,
        "all_items": all_items,
        "context_text": context_text,
        "used_uris": used_uris,
        "coverage": coverage,
        "need_external": need_external,
        "cov_reason": cov_reason,
    }


def _fetch_external_text(query: str, scope: dict, m: Metrics, cov_reason: str, degradations: list[str]) -> str:
    """Execute external search with cache and circuit breaker."""
    from . import search_cache

    cached = search_cache.get(query, scope)
    if cached:
        m.flag("cache_hit", True)
        m.step("external_search", True, {"len": len(cached), "reason": cov_reason, "cache": "hit"})
        log.info("STEP 4a 缓存命中: %d chars", len(cached))
        return cached
    m.flag("cache_hit", False)
    try:
        txt = external_search(query, scope)
        m.step("external_search", True, {"len": len(txt), "reason": cov_reason, "cache": "miss"})
        log.info("STEP 4a 外搜完成: %d chars", len(txt))
        if txt:
            search_cache.put(query, scope, txt)
        return txt
    except Exception as e:
        from .circuit_breaker import CircuitOpenError

        degradations.append(
            "external_search: circuit breaker open, search skipped"
            if isinstance(e, CircuitOpenError)
            else f"external_search: failed ({type(e).__name__}), using OV results only"
        )
        m.flag("circuit_open", isinstance(e, CircuitOpenError))
        log.warning("STEP 4 外搜失败: %s", e)
        m.step("external_search", False, {"error": str(e)})
        return ""


def _launch_async_ingest(
    backend,
    query,
    context_text,
    external_txt,
    scope,
    used_uris,
    auto_ingest,
    m,
    feedback_data,
) -> None:
    """Spawn background thread for async judge+ingest."""
    from .async_jobs import create_job, update_job

    job_id = create_job(query, scope=scope)

    def _bg_judge_ingest(_jid=job_id):
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
                    feedback_data=feedback_data,
                )
                update_job(_jid, "success")
            except Exception as e:
                log.warning("async judge+ingest failed: %s", e)
                _log_async_failure(query, e)
                update_job(_jid, "failed", error=str(e))

    import contextvars as _cv

    _ctx = _cv.copy_context()
    threading.Thread(target=_ctx.run, args=(_bg_judge_ingest,), daemon=True).start()
    log.info("async ingest: job %s deferred to background thread", job_id)
    m.step("judge_and_conflict", False, {"reason": "async_deferred", "job_id": job_id})


_EMPTY_SEARCH: dict[str, Any] = {
    "external_text": "",
    "conflict": {"has_conflict": False, "summary": "", "points": []},
    "ingested": False,
    "cv_warnings": [],
    "async_ingest_pending": False,
}


def _search_external(
    query: str,
    backend: KnowledgeBackend,
    auto_ingest: bool,
    scope: dict[str, Any],
    rctx: dict[str, Any],
    m: Metrics,
    trace: dict[str, Any],
    feedback_data: dict | None,
    degradations: list[str],
) -> dict[str, Any]:
    """Step 4: external search + optional cross-validate/judge/ingest."""
    need_external, cov_reason = rctx["need_external"], rctx["cov_reason"]
    m.flag("external_triggered", need_external)
    m.flag("external_reason", cov_reason)
    if not need_external:
        log.info("STEP 4/4 跳过外搜+冲突检测: %s", cov_reason)
        return dict(_EMPTY_SEARCH)

    context_text, used_uris = rctx["context_text"], rctx["used_uris"]
    log.info("STEP 4/4 外部搜索... reason=%s", cov_reason)
    external_txt = _fetch_external_text(query, scope, m, cov_reason, degradations)
    if not external_txt:
        return dict(_EMPTY_SEARCH)
    if ASYNC_INGEST and auto_ingest:
        _launch_async_ingest(
            backend, query, context_text, external_txt, scope, used_uris, auto_ingest, m, feedback_data
        )
        return dict(_EMPTY_SEARCH) | {"external_text": external_txt, "async_ingest_pending": True}

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
        feedback_data=feedback_data,
    )
    if judge_out.pop("judge_degraded", False):
        degradations.append("judge: LLM call failed, external content not reviewed/ingested")
    return judge_out


def _build_meta(
    rctx: dict[str, Any],
    sout: dict[str, Any],
    report: dict,
    degradations: list[str],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the meta dict from retrieve/search context."""
    retrieval_result = rctx["retrieval_result"]
    return {
        "coverage": rctx["coverage"],
        "coverage_reason": rctx["cov_reason"],
        "external_triggered": report["flags"].get("external_triggered", False),
        "external_reason": rctx["cov_reason"],
        "has_conflict": sout["conflict"].get("has_conflict", False),
        "ingested": sout["ingested"],
        "async_ingest_pending": sout["async_ingest_pending"],
        "used_uris": rctx["used_uris"],
        "warnings": sout["cv_warnings"],
        "degraded": bool(degradations),
        "degraded_reasons": degradations,
        "memories_count": len(retrieval_result["memories"]),
        "resources_count": len(retrieval_result["resources"]),
        "skills_count": len(retrieval_result["skills"]),
        "decision_trace": trace,
    }


def _log_pipeline_result(query: str, scope: dict, rctx: dict, sout: dict, auto_ingest: bool, trace: dict) -> None:
    """Write pipeline run to query_log.jsonl."""
    _log_query(
        query,
        rctx["coverage"],
        rctx["need_external"],
        rctx["cov_reason"],
        rctx["used_uris"],
        trace,
        ingested=sout["ingested"],
        async_ingest_pending=sout["async_ingest_pending"],
        need_fresh=scope.get("need_fresh", False),
        has_conflict=bool(sout["conflict"].get("has_conflict", False)),
        external_len=len(sout["external_text"]),
        auto_ingest=auto_ingest,
    )


def _build_response(
    query: str,
    scope: dict[str, Any],
    result: dict[str, Any],
    backend: KnowledgeBackend,
    session_id: str | None,
    m: Metrics,
    rctx: dict[str, Any],
    sout: dict[str, Any],
    degradations: list[str],
    trace: dict[str, Any],
    auto_ingest: bool,
) -> dict:
    """Finalize feedback/session/meta/metrics and return result dict."""
    coverage, used_uris = rctx["coverage"], rctx["used_uris"]
    m.flag("has_conflict", sout["conflict"].get("has_conflict", False))
    result["external_text"], result["conflict"] = sout["external_text"], sout["conflict"]

    summary = f"检索完成: coverage={coverage:.2f}, sources={len(used_uris)}, external={'是' if rctx['need_external'] else '否'}"
    if session_id and backend.supports_sessions:
        backend.session_add_message(session_id, "assistant", summary)
        backend.session_used(session_id, list(used_uris))
        backend.session_commit(session_id)
    m.step("feedback", True)

    report = m.finalize()
    result["meta"] = _build_meta(rctx, sout, report, degradations, trace)
    result["metrics"] = {"duration_sec": report["duration_sec"], "flags": report["flags"], "scores": report["scores"]}
    result["case_path"] = (
        capture_case(query, scope, report, result["context_text"], out_dir=CASE_DIR) if CAPTURE_CASE else None
    )
    result["decision_report"] = format_report(result)

    log.info(
        "完成: %.1fs, cov=%.2f, ext=%s, llm=%d",
        report["duration_sec"],
        coverage,
        report["flags"].get("external_triggered"),
        trace["llm_calls"],
    )
    _log_pipeline_result(query, scope, rctx, sout, auto_ingest, trace)
    return result


def _empty_result(query: str, run_id: str) -> dict[str, Any]:
    """Create an empty pipeline result template."""
    return {
        "query": query,
        "run_id": run_id,
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


def _run_impl_inner(
    query: str,
    backend: KnowledgeBackend,
    auto_ingest: bool,
    session_id: str | None,
    _skip_health: bool,
    run_id: str,
) -> dict:
    """Body of _run_impl, called after context binding."""
    from . import feedback_store

    try:
        feedback_data: dict | None = feedback_store.load()
    except Exception:
        feedback_data = None

    m = Metrics()
    result = _empty_result(query, run_id)
    degradations: list[str] = []
    trace = {"load_stage": "none", "llm_calls": 0, "external_reason": "not_evaluated"}

    scope = _route_query(query, backend, session_id, _skip_health, m, result)
    if scope is None:
        return result

    rctx = _retrieve_context(backend, query, session_id, m, result, trace, feedback_data)
    sout = _search_external(query, backend, auto_ingest, scope, rctx, m, trace, feedback_data, degradations)
    return _build_response(query, scope, result, backend, session_id, m, rctx, sout, degradations, trace, auto_ingest)


def _extract_urls(text: str) -> list[str]:
    """Extract unique URLs from text, preserving order."""
    if not text:
        return []
    raw = re.findall(r"https?://[^\s)\]>\"']+", text)
    raw = [u.rstrip(".,;:!?)>\"'") for u in raw]
    out: list[str] = []
    seen = set()
    for u in raw:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


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
