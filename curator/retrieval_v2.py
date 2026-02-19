"""检索：OV session search 为主力，find 为降级。

重构后只有 3 个公开函数：
- ov_retrieve(): 主力检索，返回三路结果
- load_context(): L0→L1→L2 严格按需下钻
- assess_coverage(): 基于 OV score 评估覆盖率（简化版）
"""

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


def load_context(ov_client, items: list, query: str, max_l2: int = 2) -> tuple:
    """严格 L0→L1→L2 分层加载内容。

    流程：
    1. L0（abstract）：检索结果自带，用于快速过滤和排序
    2. L1（overview ~2k token）：对 top 结果取 overview，判断相关性
       大多数场景 L1 够用，直接作为上下文
    3. L2（read 全文）：只有确认需要深入的才读，最多 max_l2 个

    不跳过 L1 直接读 L2。

    返回: (context_text, used_uris)
    """
    if not items:
        return "", []

    # 按 score 排序
    scored = sorted(items, key=lambda x: x.get("score", 0), reverse=True)

    blocks = []
    used_uris = []
    l1_count = 0
    l2_count = 0

    for item in scored[:8]:  # 最多看 8 个候选
        uri = item.get("uri", "")
        abstract = item.get("abstract", "") or ""
        score = item.get("score", 0)

        if not uri:
            continue

        # ── L0: abstract 快速过滤 ──
        # 低分且没有 abstract 的直接跳过
        if score < 0.3 and len(abstract) < 10:
            continue

        # ── L1: overview 判断相关性 ──
        overview = ""
        try:
            overview = ov_client.overview(uri)
        except Exception:
            pass

        l1_text = overview or abstract
        if not l1_text or len(l1_text) < 20:
            continue

        l1_count += 1

        # ── 判断是否需要 L2 ──
        # 高分 + top 结果才考虑 L2，且限制数量
        need_l2 = (score > 0.55 and l2_count < max_l2 and l1_count <= 3)

        if need_l2:
            # ── L2: 读全文 ──
            try:
                content = ov_client.read(uri)
                if content and len(str(content)) > 20:
                    blocks.append(f"[SOURCE: {uri}]\n{str(content)[:1500]}")
                    used_uris.append(uri)
                    l2_count += 1
                    continue
            except Exception:
                pass

        # L2 没读到或不需要，用 L1
        blocks.append(f"[SOURCE: {uri}]\n{l1_text[:1000]}")
        used_uris.append(uri)

    context_text = "\n\n".join(blocks)
    log.info("context 加载: %d 个源, L1=%d, L2=%d, %d chars",
             len(used_uris), l1_count - l2_count, l2_count, len(context_text))
    return context_text, used_uris


def assess_coverage(result: dict, query: str = "") -> tuple:
    """基于 OV score 评估覆盖率（简化版）。

    信任 OV 的 score，不做关键词匹配、feedback/trust/freshness 加权。
    OV 自身有 active_count 等机制来调整 score。

    规则：
    - top_score > 0.55 且 count >= 2 → local_sufficient
    - top_score > 0.45 且 count >= 1 → local_marginal
    - 否则 → need_external

    返回: (coverage: float, need_external: bool, reason: str)
    """
    all_items = result.get("all_items", [])

    if not all_items:
        return 0.0, True, "no_results"

    scored_items = [x for x in all_items if x.get("score", 0) > 0]
    if not scored_items:
        return 0.0, True, "no_scores"

    scores = [x.get("score", 0) for x in scored_items]
    avg_score = sum(scores) / len(scores)
    top_score = max(scores)
    count = len(scores)

    # 简单判断：信任 OV score
    if top_score > 0.55 and count >= 2:
        coverage = min(1.0, avg_score + 0.2)
        reason = "local_sufficient"
        need_external = False
    elif top_score > 0.45 and count >= 1:
        coverage = avg_score
        reason = "local_marginal"
        need_external = False
    elif top_score > 0.35 and count >= 1:
        coverage = avg_score * 0.8
        reason = "low_coverage"
        need_external = True
    else:
        coverage = avg_score * 0.5
        reason = "insufficient"
        need_external = True

    return coverage, need_external, reason
