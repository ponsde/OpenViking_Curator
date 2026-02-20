"""Search: external search + cross-validation (risk tagging only)."""

import json
import re

from .config import log, chat, OAI_BASE, OAI_KEY, JUDGE_MODELS


def external_search(query: str, scope: dict):
    """External search via pluggable provider (default: Grok)."""
    from search_providers import search as provider_search
    return provider_search(query, scope)


def cross_validate(query: str, external_text: str, scope: dict) -> dict:
    """标记外搜结果中的风险点（不做链式追问）。

    返回: {"validated": str, "warnings": list}
    """
    import datetime
    today = datetime.date.today().isoformat()

    prompt = (
        f"当前日期: {today}\n\n"
        f"以下是关于「{query}」的外部搜索结果:\n{external_text[:3000]}\n\n"
        "请识别其中的「易变声明」——可能已过时或需要验证的技术事实。\n"
        "重点关注:\n"
        "- API 端点、注册/认证流程（经常变）\n"
        "- 超过 6 个月前的技术声明\n"
        "- 多个来源之间矛盾的说法\n\n"
        "输出严格 JSON: {\"claims\": [{\"claim\": \"...\", \"risk\": \"high/medium/low\"}], \"summary\": \"...\"}\n"
        "如果没有风险点，输出 {\"claims\": [], \"summary\": \"无明显风险\"}"
    )

    try:
        out = None
        for model in JUDGE_MODELS:
            try:
                out = chat(OAI_BASE, OAI_KEY, model, [
                    {"role": "system", "content": "你是信息验证器。识别需要验证的易变技术声明。只输出JSON。"},
                    {"role": "user", "content": prompt},
                ], timeout=45)
                break
            except Exception as e:
                log.warning("cross_validate model %s failed: %s", model, e)
                continue

        if not out:
            return {"validated": external_text, "warnings": []}

        match = re.search(r"\{[\s\S]*\}", out)
        if not match:
            return {"validated": external_text, "warnings": []}

        result = json.loads(match.group(0))
        claims = result.get("claims", [])
        warnings = []
        for c in claims:
            risk = c.get("risk", "low")
            claim_text = c.get("claim", "")
            if not claim_text:
                continue
            if risk == "high":
                warnings.append(f"[⚠️ high] {claim_text}")
            elif risk == "medium":
                warnings.append(f"[❓ medium] {claim_text}")

        return {"validated": external_text, "warnings": warnings}

    except Exception as e:
        log.warning("cross_validate 异常: %s", e)
        return {"validated": external_text, "warnings": []}
