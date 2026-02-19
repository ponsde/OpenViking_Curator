"""检索：OV session search 为主力，find 为降级。

重构后只有 3 个公开函数：
- ov_retrieve(): 主力检索，返回三路结果
- load_context(): L0→L1→L2 分层加载
- assess_coverage(): 基于 OV score 评估覆盖率
"""

import os
import re
from .config import log


def ov_retrieve(session_mgr, query: str, limit: int = 10) -> dict:
    """主力检索：通过 SessionManager 调 OV search。

    返回:
        {
            "memories": [...],
            "resources": [...],
            "skills": [...],
            "query_plan": {...} or None,
            "all_items": [...]  # 三路合并的扁平列表
        }
    """
    result = session_mgr.search(query, limit=limit)

    memories = result.get("memories", []) or []
    resources = result.get("resources", []) or []
    skills = result.get("skills", []) or []
    all_items = memories + resources + skills

    log.info("OV 检索: memories=%d, resources=%d, skills=%d",
             len(memories), len(resources), len(skills))

    if result.get("query_plan"):
        qp = result["query_plan"]
        queries = qp.get("queries", [])
        log.info("query_plan: %d 个子查询", len(queries))
        for q in queries[:3]:
            log.debug("  [%s] %s", q.get("context_type", "?"), q.get("query", ""))

    return {
        "memories": memories,
        "resources": resources,
        "skills": skills,
        "query_plan": result.get("query_plan"),
        "all_items": all_items,
    }


def load_context(ov_client, items: list, query: str, max_l2: int = 3) -> tuple:
    """分层加载内容。

    1. 结果自带 abstract（L0）— 已在 items 里
    2. top 结果取 overview（L1）判断相关性
    3. 最相关的才 read（L2）

    返回: (context_text, used_uris)
    """
    if not items:
        return "", []

    # 按 score 排序
    scored = sorted(items, key=lambda x: x.get("score", 0), reverse=True)

    # 提取 query 核心词用于相关性判断
    q_terms = set(re.findall(r"[a-z0-9_\-]{3,}", query.lower()))
    q_cn = set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    check_terms = q_terms | q_cn

    blocks = []
    used_uris = []
    l2_count = 0

    for item in scored[:8]:  # 最多看 8 个候选
        uri = item.get("uri", "")
        abstract = item.get("abstract", "") or ""
        score = item.get("score", 0)

        if not uri:
            continue

        # L1: overview 判断相关性
        overview = ""
        try:
            overview = ov_client.overview(uri)
        except Exception:
            pass

        # 如果 overview 为空，降级用 abstract
        check_text = (overview or abstract).lower()

        # 核心词命中检查
        if check_terms:
            hits = sum(1 for t in check_terms if t.lower() in check_text)
            if hits == 0 and score < 0.5:
                continue  # 低分且核心词不命中，跳过

        # L2: 读全文（限制数量）
        if l2_count < max_l2:
            try:
                content = ov_client.read(uri)
                if content and len(str(content)) > 20:
                    blocks.append(f"[SOURCE: {uri}]\n{str(content)[:1500]}")
                    used_uris.append(uri)
                    l2_count += 1
                    continue
            except Exception:
                pass

        # 没读到 L2 就用 L1
        if overview and len(overview) > 20:
            blocks.append(f"[SOURCE: {uri}]\n{overview[:1000]}")
            used_uris.append(uri)

    context_text = "\n\n".join(blocks)
    log.info("context 加载: %d 个源, %d L2, %d chars", len(used_uris), l2_count, len(context_text))
    return context_text, used_uris


def assess_coverage(result: dict) -> tuple:
    """基于 OV 返回的 score 和数量评估覆盖率。

    返回: (coverage: float, need_external: bool, reason: str)
    """
    all_items = result.get("all_items", [])

    if not all_items:
        return 0.0, True, "no_results"

    scores = [x.get("score", 0) for x in all_items if x.get("score", 0) > 0]
    if not scores:
        return 0.0, True, "no_scores"

    avg_score = sum(scores) / len(scores)
    top_score = max(scores)
    count = len(scores)

    # 评估逻辑：
    # - top_score > 0.6 且 count >= 3 → 本地够用
    # - top_score > 0.5 且 count >= 2 → 勉强够用
    # - 否则 → 需要外搜
    if top_score > 0.6 and count >= 3:
        coverage = min(1.0, avg_score + 0.2)
        return coverage, False, "local_sufficient"
    elif top_score > 0.5 and count >= 2:
        coverage = avg_score
        return coverage, False, "local_marginal"
    elif top_score > 0.4 and count >= 1:
        coverage = avg_score * 0.8
        return coverage, True, "low_coverage"
    else:
        return avg_score * 0.5, True, "insufficient"
