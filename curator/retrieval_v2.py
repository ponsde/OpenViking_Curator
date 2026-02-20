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
    """严格按需 L0→L1→L2 分层加载。

    默认行为：
    - 先只用 L0（abstract）构造上下文；
    - 仅当 L0 不足时，才取 L1（overview）；
    - 仅当 L1 仍不足时，才取 L2（read，最多 max_l2）。
    """
    if not items:
        return "", []

    scored = sorted(items, key=lambda x: x.get("score", 0), reverse=True)

    # ---------- Stage 1: L0 only ----------
    l0_blocks = []
    l0_uris = []
    for item in scored[:4]:
        uri = item.get("uri", "")
        abstract = (item.get("abstract", "") or "").strip()
        if not uri or len(abstract) < 20:
            continue
        l0_blocks.append(f"[SOURCE: {uri}]\n{abstract[:350]}")
        l0_uris.append(uri)

    top_score = scored[0].get("score", 0) if scored else 0
    l0_enough = top_score >= 0.62 and len(l0_blocks) >= 2
    if l0_enough:
        context_text = "\n\n".join(l0_blocks)
        log.info("context 加载: stage=L0 only, sources=%d, chars=%d", len(l0_uris), len(context_text))
        return context_text, l0_uris

    # ---------- Stage 2: L1 on demand ----------
    blocks = []
    used_uris = []
    l1_count = 0
    for item in scored[:5]:
        uri = item.get("uri", "")
        if not uri:
            continue

        overview = ""
        try:
            overview = ov_client.overview(uri)
        except Exception:
            pass

        text = (overview or item.get("abstract", "") or "").strip()
        if len(text) < 20:
            continue

        blocks.append(f"[SOURCE: {uri}]\n{text[:1000]}")
        used_uris.append(uri)
        l1_count += 1

    l1_enough = top_score >= 0.5 and l1_count >= 2
    if l1_enough or max_l2 <= 0:
        context_text = "\n\n".join(blocks)
        log.info("context 加载: stage=L1, sources=%d, L2=0, chars=%d", len(used_uris), len(context_text))
        return context_text, used_uris

    # ---------- Stage 3: L2 only when still insufficient ----------
    l2_count = 0
    for item in scored[:3]:
        if l2_count >= max_l2:
            break

        uri = item.get("uri", "")
        score = item.get("score", 0)
        if not uri or score < 0.5:
            continue

        try:
            # 只升级已在 L1 阶段收录的 source
            if uri not in used_uris:
                continue
            content = ov_client.read(uri)
            if content and len(str(content)) > 20:
                pos = used_uris.index(uri)
                blocks[pos] = f"[SOURCE: {uri}]\n{str(content)[:1500]}"
                l2_count += 1
        except Exception:
            pass

    context_text = "\n\n".join(blocks)
    log.info("context 加载: stage=L2 fallback, sources=%d, L2=%d, chars=%d", len(used_uris), l2_count, len(context_text))
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
