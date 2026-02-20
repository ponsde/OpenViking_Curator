"""Pipeline v2: 返回结构化数据，不生成回答。

通过 KnowledgeBackend 接口与知识库交互，默认使用 OpenViking 后端。
可替换为 Milvus / Qdrant / Chroma / pgvector 等任何实现了 KnowledgeBackend 的后端。
"""

import os
import json
import time
from datetime import datetime, timezone

from metrics import Metrics
from memory_capture import capture_case

from .config import log, validate_config, OPENVIKING_CONFIG_FILE, DATA_PATH
from .router import route_scope
from .retrieval_v2 import ov_retrieve, load_context, assess_coverage
from .session_manager import OVClient, SessionManager
from .search import external_search, cross_validate
from .review import judge_and_ingest, detect_conflict


def _init_backend():
    """Initialize the knowledge backend. Uses OV by default."""
    from .backend_ov import OpenVikingBackend
    return OpenVikingBackend()


def _init_session_manager() -> tuple:
    """初始化 OV 客户端和 session manager。自动选嵌入/HTTP模式。"""
    ov = OVClient()  # 根据 OV_BASE_URL env 自动选模式

    if not ov.health():
        raise RuntimeError(f"OV 不可用 (mode={ov.mode})")

    sid_file = os.path.join(DATA_PATH, ".curator_session_id")
    sm = SessionManager(ov, sid_file)
    return ov, sm


def run(query: str, client=None, auto_ingest: bool = True) -> dict:
    """Main pipeline v2 — 返回结构化数据，调用方自己组装 LLM 上下文。

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
    }

    # ── decision_trace: 记录关键决策路径 ──
    trace = {
        "load_stage": "none",
        "llm_calls": 0,
        "external_reason": "not_evaluated",
    }

    # ── Step 1: 初始化 + 路由 ──
    log.info("STEP 1/4 初始化 + 路由...")
    try:
        ov, sm = _init_session_manager()
    except Exception as e:
        log.error("OV 初始化失败: %s", e)
        result["meta"]["error"] = f"知识库服务不可用: {e}"
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
    m.step("retrieve", True, {
        "memories": len(retrieval_result["memories"]),
        "resources": len(retrieval_result["resources"]),
        "skills": len(retrieval_result["skills"]),
        "total": len(all_items),
    })

    result["ov_results"] = retrieval_result

    # ── Step 3: 分层加载 + 覆盖率评估 ──
    log.info("STEP 3/4 加载内容...")
    context_text, used_uris, load_stage = load_context(ov, all_items, query, max_l2=2)
    coverage, need_external, cov_reason = assess_coverage(retrieval_result, query=query)
    m.step("load_context", True, {"coverage": coverage, "used_uris": len(used_uris), "reason": cov_reason})
    m.score("coverage_before_external", round(coverage, 3))
    log.info("STEP 3 完成: coverage=%.2f, reason=%s, stage=%s, used=%d",
             coverage, cov_reason, load_stage, len(used_uris))

    trace["load_stage"] = load_stage
    trace["external_reason"] = cov_reason

    result["context_text"] = context_text
    result["coverage"] = coverage

    # ── Step 4: 外搜 + 审核 + 冲突（可选，合并 LLM 调用）──
    external_txt = ""
    ingested = False
    cv_warnings = []
    conflict = {"has_conflict": False, "summary": "", "points": []}

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
            # B3: cross_validate 只在 need_fresh 时跑
            if scope.get("need_fresh"):
                cv = cross_validate(query, external_txt, scope)
                external_txt = cv.get("validated", external_txt)
                cv_warnings = cv.get("warnings", [])
                trace["llm_calls"] += 1
                m.step("cross_validate", True, {"warnings": len(cv_warnings)})
            else:
                m.step("cross_validate", False, {"reason": "skipped_not_fresh"})

            # B2: judge + conflict 合并为一次 LLM 调用
            judge_result = judge_and_ingest(
                ov, query, context_text, external_txt,
            )
            trace["llm_calls"] += 1
            m.step("judge_and_conflict", True, {
                "pass": judge_result.get("pass"),
                "trust": judge_result.get("trust"),
                "has_conflict": judge_result.get("has_conflict"),
            })

            conflict = {
                "has_conflict": judge_result.get("has_conflict", False),
                "summary": judge_result.get("conflict_summary", ""),
                "points": judge_result.get("conflict_points", []),
                "resolution": _resolve_conflict(judge_result),
            }

            if judge_result.get("pass") and judge_result.get("markdown"):
                freshness = judge_result.get("freshness", "unknown")
                if freshness != "outdated":
                    # M5: 冲突检测结果影响入库决策
                    # 只有 preferred == "external" 或没冲突时才自动入库
                    conflict_preferred = conflict.get("resolution", {}).get("preferred", "none")
                    if conflict_preferred in ("human_review", "local"):
                        # 有冲突且不倾向外部 → 不自动入库
                        m.step("ingest", False, {
                            "reason": f"conflict_blocked:{conflict_preferred}",
                            "conflict_summary": conflict.get("summary", ""),
                        })
                        log.info("冲突阻止入库: preferred=%s, summary=%s",
                                 conflict_preferred, conflict.get("summary", ""))
                    elif auto_ingest:
                        try:
                            from .review import ingest_markdown_v2
                            ing = ingest_markdown_v2(ov, query[:60], judge_result["markdown"], freshness=freshness)
                            ingested = True
                            m.step("ingest", True, {"uri": ing.get("root_uri", "")})
                            log.info("已入库: %s", ing.get("root_uri", ""))

                            # C1: 入库后轻量验证
                            _verify_ingest(ov, query, ing.get("root_uri", ""), m)
                        except Exception as e:
                            m.step("ingest", False, {"error": str(e)})
                    else:
                        # Review mode: mark as pending, don't auto-ingest
                        m.step("ingest", False, {"reason": "review_mode_pending"})
                        log.info("审核模式: 内容待人工确认，未自动入库")
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
        case_path = capture_case(query, scope, report, context_text,
                                 out_dir=os.getenv("CURATOR_CASE_DIR", "cases"))

    result["meta"] = {
        "coverage": coverage,
        "coverage_reason": cov_reason,
        "external_triggered": report["flags"].get("external_triggered", False),
        "external_reason": cov_reason,
        "has_conflict": conflict.get("has_conflict", False),
        "ingested": ingested,
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

    log.info("完成: %.1fs, coverage=%.2f, external=%s, llm_calls=%d",
             report["duration_sec"], coverage,
             report["flags"].get("external_triggered"), trace["llm_calls"])

    # 写 query 日志
    _log_query(query, coverage, need_external, cov_reason, used_uris, trace)

    return result


def _log_query(query: str, coverage: float, need_external: bool,
               reason: str, used_uris: list, trace: dict) -> None:
    """写 query 日志到 data/query_log.jsonl（append 模式，失败不影响主流程）。"""
    try:
        log_dir = DATA_PATH
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "query_log.jsonl")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "coverage": round(coverage, 4),
            "external_triggered": bool(need_external),
            "reason": reason,
            "used_uris": list(used_uris) if used_uris else [],
            "load_stage": trace.get("load_stage", "unknown"),
            "llm_calls": trace.get("llm_calls", 0),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("query log 写入失败（不影响主流程）: %s", e)


def _verify_ingest(ov: OVClient, query: str, new_uri: str, m: Metrics):
    """C1: 入库后轻量验证 — 检查新 URI 是否出现在检索结果中。"""
    if not new_uri:
        return
    try:
        check = ov.find(query, limit=5)
        found_uris = [r.get("uri", "") for r in check.get("resources", [])]
        hit = any(new_uri in u for u in found_uris)
        m.step("ingest_verify", True, {"hit": hit, "new_uri": new_uri})
        if hit:
            log.info("入库验证通过: %s", new_uri)
        else:
            log.warning("入库验证未命中（可能需要更长索引时间）: %s", new_uri)
    except Exception as e:
        m.step("ingest_verify", False, {"error": str(e)})


def _resolve_conflict(judge_result: dict) -> dict:
    """Conflict resolution strategy.

    When local and external sources contradict, decide which to trust:
    - trust > 7 + freshness=current → prefer external
    - trust < 4 → prefer local
    - otherwise → flag for human review

    Returns:
        {"strategy": str, "preferred": "local"|"external"|"human_review", "reason": str}
    """
    if not judge_result.get("has_conflict"):
        return {"strategy": "no_conflict", "preferred": "none", "reason": ""}

    trust = judge_result.get("trust", 5)
    freshness = judge_result.get("freshness", "unknown")

    strategy = os.environ.get("CURATOR_CONFLICT_STRATEGY", "auto")

    if strategy == "local":
        return {"strategy": "local_always", "preferred": "local", "reason": "config: always prefer local"}
    elif strategy == "external":
        return {"strategy": "external_always", "preferred": "external", "reason": "config: always prefer external"}
    elif strategy == "human":
        return {"strategy": "human_always", "preferred": "human_review", "reason": "config: always human review"}

    # Auto strategy: decide based on trust + freshness
    if trust >= 7 and freshness in ("current", "recent"):
        return {"strategy": "auto", "preferred": "external", "reason": f"high trust ({trust}/10) + fresh ({freshness})"}
    elif trust <= 3:
        return {"strategy": "auto", "preferred": "local", "reason": f"low trust ({trust}/10), prefer local knowledge"}
    else:
        return {"strategy": "auto", "preferred": "human_review", "reason": f"medium trust ({trust}/10), needs human judgment"}
