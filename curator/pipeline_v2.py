"""Pipeline v2: OV-native pipeline — 返回结构化数据，不生成回答。"""

import os
import json
import time

from metrics import Metrics
from memory_capture import capture_case

from .config import log, validate_config, OPENVIKING_CONFIG_FILE, DATA_PATH
from .router import route_scope
from .retrieval_v2 import ov_retrieve, load_context, assess_coverage
from .session_manager import OVClient, SessionManager
from .search import external_boost_needed, external_search, cross_validate
from .review import judge_and_pack, ingest_markdown_v2, detect_conflict


def _init_session_manager() -> tuple:
    """初始化 OV HTTP 客户端和 session manager。"""
    base_url = os.environ.get("OV_BASE_URL", "http://127.0.0.1:9100")
    ov = OVClient(base_url)

    if not ov.health():
        raise RuntimeError(f"OV serve 不可用: {base_url}")

    sid_file = os.path.join(DATA_PATH, ".curator_session_id")
    sm = SessionManager(ov, sid_file)
    return ov, sm


def run(query: str, client=None) -> dict:
    """Main pipeline v2 — 返回结构化数据，调用方自己组装 LLM 上下文。

    返回:
        {
            "query": str,
            "ov_results": dict,         # 原始检索结果
            "context_text": str,         # 分层加载的上下文内容
            "external_text": str,        # 外搜补充内容（可能为空）
            "coverage": float,           # 覆盖率评分
            "conflict": dict,            # 冲突检测结果
            "meta": dict,                # 元信息
            "metrics": dict,             # 性能指标
            "case_path": str | None,     # case 文件路径
        }
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

    # ── Step 1: 初始化 + 路由 ──
    log.info("STEP 1/5 初始化 + 路由...")
    try:
        ov, sm = _init_session_manager()
    except Exception as e:
        log.error("OV 初始化失败: %s", e)
        result["meta"]["error"] = f"知识库服务不可用: {e}"
        return result

    m.step("init", True)

    scope = route_scope(query)
    m.step("route", True, {"domain": scope.get("domain"), "confidence": scope.get("confidence")})
    log.info("STEP 1 完成: domain=%s, confidence=%s", scope.get("domain"), scope.get("confidence"))

    # 记录用户提问到 session
    sm.add_user_query(query)

    # ── Step 2: OV 检索 ──
    log.info("STEP 2/5 OV 检索...")
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
    log.info("STEP 3/5 加载内容...")
    context_text, used_uris = load_context(ov, all_items, query, max_l2=2)
    coverage, need_external, cov_reason = assess_coverage(retrieval_result, query=query)
    m.step("load_context", True, {"coverage": coverage, "used_uris": len(used_uris), "reason": cov_reason})
    m.score("coverage_before_external", round(coverage, 3))
    log.info("STEP 3 完成: coverage=%.2f, reason=%s, used=%d", coverage, cov_reason, len(used_uris))

    result["context_text"] = context_text
    result["coverage"] = coverage

    # ── Step 4: 外搜（可选）──
    external_txt = ""
    ingested = False
    cv_warnings = []

    if need_external:
        m.flag("external_triggered", True)
        m.flag("external_reason", cov_reason)
        log.info("STEP 4/5 外部搜索... reason=%s", cov_reason)
        try:
            external_txt = external_search(query, scope)
            m.step("external_search", True, {"len": len(external_txt), "reason": cov_reason})
            log.info("STEP 4 完成: %d chars", len(external_txt))
        except Exception as e:
            log.warning("STEP 4 外搜失败: %s", e)
            m.step("external_search", False, {"error": str(e)})

        if external_txt:
            # 交叉验证
            cv = cross_validate(query, external_txt, scope)
            external_txt = cv.get("validated", external_txt)
            cv_warnings = cv.get("warnings", [])

            # 审核入库
            j = judge_and_pack(query, external_txt)
            m.step("judge", True, {"pass": j.get("pass"), "trust": j.get("trust")})
            if j.get("pass") and j.get("markdown"):
                freshness = j.get("freshness", "unknown")
                if freshness != "outdated":
                    try:
                        ing = ingest_markdown_v2(ov, "curated", j["markdown"], freshness=freshness)
                        ingested = True
                        m.step("ingest", True, {"uri": ing.get("root_uri", "")})
                        log.info("已入库: %s", ing.get("root_uri", ""))
                    except Exception as e:
                        m.step("ingest", False, {"error": str(e)})
    else:
        m.flag("external_triggered", False)
        m.flag("external_reason", cov_reason)
        log.info("STEP 4/5 跳过外搜: %s", cov_reason)

    result["external_text"] = external_txt

    # ── Step 5: 冲突检测 + Session 反馈 ──
    log.info("STEP 5/5 冲突检测 + session 反馈...")

    conflict = detect_conflict(query, context_text, external_txt)
    m.flag("has_conflict", bool(conflict.get("has_conflict", False)))
    result["conflict"] = conflict

    # Session 反馈：记录结构化摘要（不再记录 LLM 生成的回答）
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
        "has_conflict": report["flags"].get("has_conflict", False),
        "ingested": ingested,
        "used_uris": used_uris,
        "warnings": cv_warnings,
        "memories_count": len(retrieval_result["memories"]),
        "resources_count": len(retrieval_result["resources"]),
        "skills_count": len(retrieval_result["skills"]),
    }
    result["metrics"] = {
        "duration_sec": report["duration_sec"],
        "flags": report["flags"],
        "scores": report["scores"],
    }
    result["case_path"] = case_path

    log.info("完成: %.1fs, coverage=%.2f, external=%s",
             report["duration_sec"], coverage, report["flags"].get("external_triggered"))

    return result
