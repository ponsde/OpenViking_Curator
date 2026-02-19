"""Search: external boost decision, external search, cross-validation."""

import json
import datetime
import re

from .config import (
    env, log, chat,
    OAI_BASE, OAI_KEY, JUDGE_MODELS,
    THRESHOLD_LOW_COV, THRESHOLD_LOW_COV_INTERNAL,
    THRESHOLD_CORE_COV, THRESHOLD_LOW_TRUST, THRESHOLD_LOW_FRESH,
)

def external_boost_needed(query: str, scope: dict, coverage: float, meta: dict):
    q = (query or "").lower()
    need_fresh = bool(scope.get("need_fresh", False)) or any(k in q for k in ["最新", "更新", "release", "changelog", "2026", "2025"])
    low_quality = meta.get("avg_top_trust", 0) < THRESHOLD_LOW_TRUST
    low_fresh = meta.get("fresh_ratio", 0) < THRESHOLD_LOW_FRESH
    weak_feedback = meta.get("max_feedback_score", 0) <= 0
    core_cov = meta.get("core_cov", 1.0)

    # 覆盖率阈值（已知内部域名可更宽松，减少重复外搜）
    low_cov_threshold = THRESHOLD_LOW_COV
    if any(k in q for k in ["newapi", "openviking", "grok2api", "mcp"]):
        low_cov_threshold = THRESHOLD_LOW_COV_INTERNAL

    if coverage < low_cov_threshold:
        return True, "low_coverage"
    # 核心词覆盖低 = 知识库对这个话题实际没覆盖，即使通用词拉高了 coverage
    if core_cov <= THRESHOLD_CORE_COV:
        return True, "low_core_coverage"
    if need_fresh and (low_fresh or low_quality):
        return True, "freshness_or_quality_boost"
    if need_fresh and weak_feedback and low_quality:
        return True, "need_fresh_no_positive_feedback"
    return False, "local_sufficient"


def external_search(query: str, scope: dict):
    """External search via pluggable provider (default: Grok)."""
    from search_providers import search as provider_search
    return provider_search(query, scope)


def cross_validate(query: str, external_text: str, scope: dict) -> dict:
    """P0: 交叉验证 + 链式搜索
    检测外搜结果中的易变声明，自动追问验证。
    返回: {"validated": str, "warnings": list, "followup_done": bool}
    """
    import datetime
    today = datetime.date.today().isoformat()

    # 第一步：用 LLM 识别外搜结果中需要验证的声明
    extract_prompt = (
        f"当前日期: {today}\n\n"
        f"以下是关于「{query}」的外部搜索结果:\n{external_text[:3000]}\n\n"
        "请识别其中的「易变声明」——即可能已经过时或需要验证的技术事实。\n"
        "重点关注:\n"
        "- API端点、注册/认证流程、验证要求（这些经常变）\n"
        "- 来自超过6个月前的项目的技术声明\n"
        "- 多个来源之间互相矛盾的说法\n"
        "- 把某个项目的特定实现当成通用事实的情况\n\n"
        "输出严格JSON: {\"claims\": [{\"claim\": \"...\", \"source_date\": \"...\", \"risk\": \"high/medium/low\"}], "
        "\"needs_followup\": bool, \"followup_query\": \"如果needs_followup=true，给出验证搜索词\"}"
    )

    try:
        # 尝试多个模型，防止单点 503
        cv_models = (JUDGE_MODELS if JUDGE_MODELS else []) + ["gemini-3-flash-preview"]
        out = None
        for cv_model in cv_models:
            try:
                out = chat(OAI_BASE, OAI_KEY, cv_model, [
                    {"role": "system", "content": "你是信息验证器。识别需要交叉验证的易变技术声明。只输出JSON。"},
                    {"role": "user", "content": extract_prompt},
                ], timeout=45)
                break
            except Exception as e:
                log.warning("cross_validate model %s failed: %s", cv_model, e)
                continue

        if not out:
            return {"validated": external_text, "warnings": [], "followup_done": False}

        match = re.search(r"\{[\s\S]*\}", out)
        if not match:
            return {"validated": external_text, "warnings": [], "followup_done": False}

        result = json.loads(match.group(0))
        claims = result.get("claims", [])
        high_risk = [c for c in claims if c.get("risk") == "high"]
        warnings = [c.get("claim", "") for c in high_risk]

        # 第二步：如果有高风险声明且建议追问，做链式搜索
        followup_text = ""
        if result.get("needs_followup") and result.get("followup_query") and high_risk:
            log.info("交叉验证追问: %s", result.get("followup_query"))
            try:
                from search_providers import get_provider, _build_search_prompt
                provider_fn = get_provider()
                followup_text = provider_fn(
                    result['followup_query'],
                    {"keywords": [c.get('claim','')[:30] for c in high_risk], "exclude": [], "source_pref": ["official_docs"]},
                )
                log.info("追问完成: %d chars", len(followup_text))
            except Exception as e:
                log.warning("追问失败: %s", e)

        # 合并结果
        validated = external_text
        if followup_text:
            validated = (
                external_text +
                "\n\n--- 交叉验证补充 ---\n" +
                followup_text
            )

        return {
            "validated": validated,
            "warnings": warnings,
            "followup_done": bool(followup_text),
            "high_risk_count": len(high_risk),
        }

    except Exception as e:
        log.warning("交叉验证异常: %s", e)
        return {"validated": external_text, "warnings": [], "followup_done": False}


