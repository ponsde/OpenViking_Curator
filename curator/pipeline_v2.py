"""Pipeline v2: OV-native 6-step pipeline。"""

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
from .answer import answer, _build_source_footer


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
    """Main pipeline v2. OV session search 为主力。

    Args:
        query: 用户查询
        client: 兼容旧接口，传入时忽略（v2 用 HTTP API）
    """
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    result = {"query": query, "answer": "", "meta": {}, "metrics": {}, "case_path": None}

    # ── Step 1: 初始化 + 路由 ──
    log.info("STEP 1/6 初始化 + 路由...")
    try:
        ov, sm = _init_session_manager()
    except Exception as e:
        log.error("OV 初始化失败: %s", e)
        result["answer"] = f"知识库服务不可用: {e}"
        return result

    m.step("init", True)

    scope = route_scope(query)
    m.step("route", True, {"domain": scope.get("domain"), "confidence": scope.get("confidence")})
    log.info("STEP 1 完成: domain=%s, confidence=%s", scope.get("domain"), scope.get("confidence"))

    # 记录用户提问到 session
    sm.add_user_query(query)

    # ── Step 2: OV 检索 ──
    log.info("STEP 2/6 OV 检索...")
    retrieval_result = ov_retrieve(sm, query, limit=10)
    all_items = retrieval_result["all_items"]
    m.step("retrieve", True, {
        "memories": len(retrieval_result["memories"]),
        "resources": len(retrieval_result["resources"]),
        "skills": len(retrieval_result["skills"]),
        "total": len(all_items),
    })

    # ── Step 3: 分层加载 + 覆盖率评估 ──
    log.info("STEP 3/6 加载内容...")
    context_text, used_uris = load_context(ov, all_items, query, max_l2=3)
    coverage, need_external, cov_reason = assess_coverage(retrieval_result)
    m.step("load_context", True, {"coverage": coverage, "used_uris": len(used_uris), "reason": cov_reason})
    m.score("coverage_before_external", round(coverage, 3))
    log.info("STEP 3 完成: coverage=%.2f, reason=%s, used=%d", coverage, cov_reason, len(used_uris))

    # ── Step 4: 外搜（可选）──
    external_txt = ""
    ingested = False
    cv_warnings = []

    if need_external:
        m.flag("external_triggered", True)
        m.flag("external_reason", cov_reason)
        log.info("STEP 4/6 外部搜索... reason=%s", cov_reason)
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
        log.info("STEP 4/6 跳过外搜: %s", cov_reason)

    # ── Step 5: 生成回答 ──
    log.info("STEP 5/6 生成回答...")

    # 冲突检测
    conflict = detect_conflict(query, context_text, external_txt)
    conflict_card = ""
    if conflict.get("has_conflict"):
        pts = "\n".join([f"- {x}" for x in conflict.get("points", [])[:5]])
        conflict_card = f"存在冲突: {conflict.get('summary', '')}\n{pts}"
    m.flag("has_conflict", bool(conflict.get("has_conflict", False)))

    ans = answer(query, context_text, external_txt,
                 priority_ctx="", conflict_card=conflict_card, warnings=cv_warnings)

    # source footer
    meta_for_footer = {
        "uris": [x.get("uri", "") for x in all_items[:8]],
        "priority_uris": used_uris,
    }
    source_info = _build_source_footer(meta_for_footer, coverage, need_external, cv_warnings)
    ans = ans.rstrip() + "\n\n" + source_info

    m.step("answer", True, {"answer_len": len(ans)})
    m.flag("ingested", ingested)

    # ── Step 6: Session 反馈 ──
    log.info("STEP 6/6 session 反馈...")
    sm.add_assistant_response(ans, used_uris)
    sm.maybe_commit()
    m.step("feedback", True)

    # ── 结果 ──
    report = m.finalize()

    case_path = None
    if os.getenv("CURATOR_CAPTURE_CASE", "1") in ("1", "true"):
        case_path = capture_case(query, scope, report, ans,
                                 out_dir=os.getenv("CURATOR_CASE_DIR", "cases"))

    result["answer"] = ans
    result["meta"] = {
        "coverage": coverage,
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
