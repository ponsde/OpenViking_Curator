"""检索：OV session search 为主力，find 为降级。

重构后只有 3 个公开函数：
- ov_retrieve(): 主力检索，返回三路结果
- load_context(): L0→L1→L2 严格按需下钻
- assess_coverage(): 基于 OV score 评估覆盖率（简化版）

所有知识库操作通过 KnowledgeBackend 接口（或 duck-typed 兼容对象）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import (
    log,
    THRESHOLD_L0_SUFFICIENT, THRESHOLD_L1_SUFFICIENT,
    THRESHOLD_COV_SUFFICIENT, THRESHOLD_COV_MARGINAL, THRESHOLD_COV_LOW,
    FEEDBACK_WEIGHT,
)

if TYPE_CHECKING:
    from .backend import KnowledgeBackend


def rerank_with_feedback(items: list) -> list:
    """用 feedback_store 的命中记录微调检索排名。

    调整公式（保守，OV 原始 score 仍主导）：
        raw_boost   = min(1.0, (up + adopt*2) / (total + 1))
        raw_penalty = min(1.0, down           / (total + 1))
        delta       = (raw_boost - raw_penalty) * FEEDBACK_WEIGHT

    delta 范围：(-FEEDBACK_WEIGHT, +FEEDBACK_WEIGHT)，即默认 (-0.10, +0.10)。
    clamp 到 1.0 后再乘权重，保证 OV 原始分始终主导排名。

    up    = 显式「有用」标记
    down  = 显式「没用」标记
    adopt = OV 结果被 pipeline 实际采用（自动记录）

    adopt 权重 ×2，因为它是 pipeline 根据实际效果自动记录的客观信号，
    比手动标记更可靠——但乘以 FEEDBACK_WEIGHT 后仍受上限约束。
    """
    if not items:
        return items

    try:
        from curator import feedback_store
        fb = feedback_store.load()
    except Exception:
        return items   # feedback_store 不可用时静默跳过，不影响检索

    if not fb:
        return items   # 没有任何 feedback 记录，直接返回

    adjusted = []
    for item in items:
        uri = item.get("uri", "")
        rec = fb.get(uri)
        if not rec:
            adjusted.append(item)
            continue

        up = rec.get("up", 0)
        down = rec.get("down", 0)
        adopt = rec.get("adopt", 0)
        total = up + down + adopt

        # clamp 信号到 [0, 1] 再乘权重，确保 delta ∈ (-WEIGHT, +WEIGHT)
        boost_signal   = min(1.0, (up + adopt * 2) / (total + 1))
        penalty_signal = min(1.0, down              / (total + 1))
        delta = round((boost_signal - penalty_signal) * FEEDBACK_WEIGHT, 4)

        new_item = dict(item)
        original = float(item.get("score", 0) or 0)
        new_item["score"] = round(original + delta, 4)
        new_item["_feedback_delta"] = delta
        adjusted.append(new_item)
        if delta:
            log.debug("feedback rerank: uri=%s delta=%+.4f (up=%d down=%d adopt=%d)",
                      uri, delta, up, down, adopt)

    # 重新按 score 降序排列
    adjusted.sort(key=lambda x: x.get("score", 0), reverse=True)
    return adjusted


def ov_retrieve(session_mgr, query: str, limit: int = 10) -> dict:
    """主力检索：通过 SessionManager 调 OV search。

    返回:
        {
            "memories": [...],
            "resources": [...],
            "skills": [...],
            "query_plan": {...} or None,
            "all_items": [...]  # 三路合并的扁平列表，已按 feedback 微调排序
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
        queries = getattr(qp, "queries", []) if not isinstance(qp, dict) else qp.get("queries", [])
        log.info("query_plan: %d 个子查询", len(queries))
        for q in queries[:3]:
            ct = getattr(q, "context_type", "?") if not isinstance(q, dict) else q.get("context_type", "?")
            qtext = getattr(q, "query", "") if not isinstance(q, dict) else q.get("query", "")
            log.debug("  [%s] %s", ct, qtext)

    # 保留原始分快照，供 assess_coverage 使用（评估覆盖率应基于 OV 原始信号）
    # 浅拷贝足够：rerank_with_feedback 不 in-place 修改 item dict（有 feedback 时新建 dict）
    all_items_raw = list(all_items)

    # feedback 微调排名（保守权重，OV 原始 score 仍主导排序和 load_context 优先级）
    all_items = rerank_with_feedback(all_items)

    return {
        "memories": memories,
        "resources": resources,
        "skills": skills,
        "query_plan": result.get("query_plan"),
        "all_items": all_items,          # feedback-adjusted，用于 load_context 排序
        "all_items_raw": all_items_raw,  # OV 原始分，用于 assess_coverage 覆盖率判断
    }


def load_context(backend, items: list, query: str, max_l2: int = 2) -> tuple:
    """严格按需 L0→L1→L2 分层加载。

    Args:
        backend: A :class:`KnowledgeBackend` instance (or any object with
                 ``overview(uri)`` and ``read(uri)`` methods).
        items: List of search result dicts with ``uri``, ``score``, ``abstract``.
        query: Original user query (for logging).
        max_l2: Maximum number of items to load at L2 (full read).

    Returns:
        Tuple of ``(context_text, used_uris, stage)``.

    默认行为：
    - 先只用 L0（abstract）构造上下文；
    - 仅当 L0 不足时，才取 L1（overview）；
    - 仅当 L1 仍不足时，才取 L2（read，最多 max_l2）。
    """
    if not items:
        return "", [], "none"

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
    l0_enough = top_score >= THRESHOLD_L0_SUFFICIENT and len(l0_blocks) >= 2
    if l0_enough:
        context_text = "\n\n".join(l0_blocks)
        log.info("context 加载: stage=L0 only, sources=%d, chars=%d", len(l0_uris), len(context_text))
        return context_text, l0_uris, "L0"

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
            overview = backend.overview(uri)
        except Exception:
            pass

        text = (overview or item.get("abstract", "") or "").strip()
        if len(text) < 20:
            continue

        blocks.append(f"[SOURCE: {uri}]\n{text[:1000]}")
        used_uris.append(uri)
        l1_count += 1

    l1_enough = top_score >= THRESHOLD_L1_SUFFICIENT and l1_count >= 2
    if l1_enough or max_l2 <= 0:
        context_text = "\n\n".join(blocks)
        log.info("context 加载: stage=L1, sources=%d, L2=0, chars=%d", len(used_uris), len(context_text))
        return context_text, used_uris, "L1"

    # ---------- Stage 3: L2 only when still insufficient ----------
    # L2 按原始 score 排序取 top N，不限制 used_uris，
    # 这样 L1 阶段因 overview 返回空而被跳过的高分 URI 也有机会被深度加载。
    l2_count = 0
    for item in scored[:3]:
        if l2_count >= max_l2:
            break

        uri = item.get("uri", "")
        score = item.get("score", 0)
        if not uri or score < THRESHOLD_L1_SUFFICIENT:
            continue

        try:
            content = backend.read(uri)
            if content and len(str(content)) > 20:
                if uri in used_uris:
                    # 升级已有的 L1 块
                    pos = used_uris.index(uri)
                    blocks[pos] = f"[SOURCE: {uri}]\n{str(content)[:1500]}"
                else:
                    # L1 阶段被跳过的高分 URI，现在补上
                    blocks.append(f"[SOURCE: {uri}]\n{str(content)[:1500]}")
                    used_uris.append(uri)
                l2_count += 1
        except Exception:
            pass

    context_text = "\n\n".join(blocks)
    log.info("context 加载: stage=L2 fallback, sources=%d, L2=%d, chars=%d", len(used_uris), l2_count, len(context_text))
    return context_text, used_uris, "L2"


def assess_coverage(result: dict, query: str = "") -> tuple:
    """基于 OV 原始 score 评估覆盖率（简化版）。

    使用 result["all_items_raw"]（OV 原始分，未经 feedback 调整），
    确保覆盖率判断和是否触发外搜只依赖 OV 自身信号，不受 feedback 影响。
    若 all_items_raw 不存在（旧调用兼容），退回到 all_items。

    规则：
    - top_score > 0.55 且 count >= 2 → local_sufficient
    - top_score > 0.45 且 count >= 1 → local_marginal
    - 否则 → need_external

    返回: (coverage: float, need_external: bool, reason: str)
    """
    # 优先用原始分，保证外搜决策不受 feedback 影响
    all_items = result.get("all_items_raw") or result.get("all_items", [])

    if not all_items:
        return 0.0, True, "no_results"

    scored_items = [x for x in all_items if x.get("score", 0) > 0]
    if not scored_items:
        return 0.0, True, "no_scores"

    scores = [x.get("score", 0) for x in scored_items]
    avg_score = sum(scores) / len(scores)
    top_score = max(scores)
    count = len(scores)

    # 规模修正：知识库结果数少时，OV score 分布不可信，适当放宽阈值
    # n=1 → factor≈0.78, n=3 → factor≈0.84, n≥9 → factor=1.0（恢复原始阈值）
    scale_factor = min(1.0, 0.75 + 0.03 * count)
    effective_sufficient = THRESHOLD_COV_SUFFICIENT * scale_factor
    effective_marginal   = THRESHOLD_COV_MARGINAL   * scale_factor

    # 基于有效阈值判断
    if top_score > effective_sufficient and count >= 2:
        coverage = min(1.0, avg_score + 0.2)
        reason = "local_sufficient"
        need_external = False
    elif top_score > effective_marginal and count >= 1:
        coverage = avg_score
        reason = "local_marginal"
        need_external = True   # marginal 也外搜补充
    elif top_score > THRESHOLD_COV_LOW and count >= 1:
        coverage = avg_score * 0.8
        reason = "low_coverage"
        need_external = True
    else:
        coverage = avg_score * 0.5
        reason = "insufficient"
        need_external = True

    return coverage, need_external, reason
