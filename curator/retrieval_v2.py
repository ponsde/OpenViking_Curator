"""检索：OV session search 为主力，find 为降级。

重构后只有 3 个公开函数：
- ov_retrieve(): 主力检索，返回三路结果
- load_context(): L0→L1→L2 严格按需下钻
- assess_coverage(): 基于 OV score 评估覆盖率（简化版）

所有知识库操作通过 KnowledgeBackend 接口（或 duck-typed 兼容对象）。
"""

from __future__ import annotations

from .config import (
    FEEDBACK_WEIGHT,
    THRESHOLD_COV_LOW,
    THRESHOLD_COV_MARGINAL,
    THRESHOLD_COV_SUFFICIENT,
    THRESHOLD_L0_SUFFICIENT,
    THRESHOLD_L1_SUFFICIENT,
    log,
)

# ── assess_coverage tuning constants ──
# These are internal signal-weighting parameters, not user-facing thresholds.
# User-facing thresholds (COV_SUFFICIENT etc.) live in config.py / settings.py.

# Score-gap penalty: if top1 - top2 > this, it looks like an isolated hit
_GAP_PENALTY_THRESHOLD = 0.25
_GAP_PENALTY_MULTIPLIER = 0.3  # gap * multiplier = raw penalty
_GAP_PENALTY_CAP = 0.10  # max penalty applied regardless of gap size

# Keyword-overlap penalty: fraction of query keywords found in local abstracts
_KW_OVERLAP_LOW = 0.4  # below this → larger penalty
_KW_OVERLAP_MED = 0.6  # below this (but ≥ low) → smaller penalty
_KW_PENALTY_LOW = 0.08  # penalty when overlap < _KW_OVERLAP_LOW
_KW_PENALTY_MED = 0.04  # penalty when _KW_OVERLAP_LOW ≤ overlap < _KW_OVERLAP_MED

# Scale factor: fewer KB results → OV score distribution is less reliable
_SCALE_FACTOR_BASE = 0.75  # min scale (single result)
_SCALE_FACTOR_PER_ITEM = 0.03  # added per additional result (saturates at 1.0)

# Coverage calculation adjustments per decision branch
_COV_BONUS_SUFFICIENT = 0.2  # boost when clearly sufficient (avg_score + bonus)
_COV_DISCOUNT_MARGINAL = 0.5  # gap_penalty multiplier for marginal branch
_COV_DISCOUNT_LOW = 0.8  # scale applied when coverage is low
_COV_DISCOUNT_INSUFFICIENT = 0.5  # scale applied when coverage is insufficient


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
    except Exception as e:
        log.debug("feedback_store load failed: %s", e)
        return items  # feedback_store 不可用时静默跳过，不影响检索

    if not fb:
        return items  # 没有任何 feedback 记录，直接返回

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
        boost_signal = min(1.0, (up + adopt * 2) / (total + 1))
        penalty_signal = min(1.0, down / (total + 1))
        delta = round((boost_signal - penalty_signal) * FEEDBACK_WEIGHT, 4)

        new_item = dict(item)
        original = float(item.get("score", 0) or 0)
        new_item["score"] = round(original + delta, 4)
        new_item["_feedback_delta"] = delta
        adjusted.append(new_item)
        if delta:
            log.debug("feedback rerank: uri=%s delta=%+.4f (up=%d down=%d adopt=%d)", uri, delta, up, down, adopt)

    # 重新按 score 降序排列
    adjusted.sort(key=lambda x: x.get("score", 0), reverse=True)
    return adjusted


def _search_result_to_dict(r) -> dict:
    """Normalise a SearchResult dataclass or a raw dict to a plain dict."""
    if isinstance(r, dict):
        return r
    return {
        "uri": r.uri,
        "abstract": r.abstract,
        "overview": r.overview,
        "score": r.score,
        "context_type": r.context_type,
        "match_reason": r.match_reason,
        "category": r.category,
        "relations": r.relations,
        "metadata": r.metadata,
    }


def backend_retrieve(backend, query: str, session_id: str | None = None, limit: int = 10) -> dict:
    """主力检索：通过 KnowledgeBackend 接口搜索，与后端无关。

    Args:
        backend: A :class:`KnowledgeBackend` instance.
        query: User query string.
        session_id: Optional session ID for context-aware search.
        limit: Maximum number of results to return.

    返回:
        {
            "memories": [...],
            "resources": [...],
            "skills": [...],
            "query_plan": {...} or None,
            "all_items": [...]  # 三路合并的扁平列表，已按 feedback 微调排序
        }
    """
    resp = backend.search(query, session_id=session_id, limit=limit)

    items = [_search_result_to_dict(r) for r in resp.results]

    # Split by context_type; default unclassified items to "resource"
    memories = [i for i in items if i.get("context_type") == "memory"]
    skills = [i for i in items if i.get("context_type") == "skill"]
    categorised_uris = {i["uri"] for i in memories + skills}
    resources = [i for i in items if i["uri"] not in categorised_uris]

    log.info("检索: memories=%d, resources=%d, skills=%d", len(memories), len(resources), len(skills))

    if resp.query_plan:
        qp = resp.query_plan
        queries = getattr(qp, "queries", []) if not isinstance(qp, dict) else qp.get("queries", [])
        log.info("query_plan: %d 个子查询", len(queries))
        for q in queries[:3]:
            ct = getattr(q, "context_type", "?") if not isinstance(q, dict) else q.get("context_type", "?")
            qtext = getattr(q, "query", "") if not isinstance(q, dict) else q.get("query", "")
            log.debug("  [%s] %s", ct, qtext)

    # 保留原始分快照，供 assess_coverage 使用（评估覆盖率应基于原始信号，不受 feedback 影响）
    all_items_raw = list(items)

    # feedback 微调排名（保守权重，原始 score 仍主导）
    all_items = rerank_with_feedback(items)

    return {
        "memories": memories,
        "resources": resources,
        "skills": skills,
        "query_plan": resp.query_plan,
        "all_items": all_items,
        "all_items_raw": all_items_raw,
    }


# Backward-compat alias — callers using the old OV-specific name still work.
# Deprecated: use backend_retrieve() directly.
def ov_retrieve(session_mgr_or_backend, query: str, limit: int = 10) -> dict:
    """Deprecated alias for :func:`backend_retrieve`.

    Accepts either the old ``SessionManager`` (duck-typed: must have
    ``.search(query, limit)``) or a :class:`KnowledgeBackend` instance.

    New code should call ``backend_retrieve(backend, query, session_id)``
    directly.

    .. deprecated::
        Use :func:`backend_retrieve` instead.
    """
    import warnings

    warnings.warn(
        "ov_retrieve() is deprecated, use backend_retrieve() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from .backend import KnowledgeBackend

    if isinstance(session_mgr_or_backend, KnowledgeBackend):
        return backend_retrieve(session_mgr_or_backend, query, limit=limit)

    # Legacy path: session_mgr duck-typed object with .search(query, limit)
    result = session_mgr_or_backend.search(query, limit=limit)
    memories = result.get("memories", []) or []
    resources = result.get("resources", []) or []
    skills = result.get("skills", []) or []
    all_items = memories + resources + skills

    log.info("OV 检索: memories=%d, resources=%d, skills=%d", len(memories), len(resources), len(skills))

    if result.get("query_plan"):
        qp = result["query_plan"]
        queries = getattr(qp, "queries", []) if not isinstance(qp, dict) else qp.get("queries", [])
        log.info("query_plan: %d 个子查询", len(queries))

    all_items_raw = list(all_items)
    all_items = rerank_with_feedback(all_items)

    return {
        "memories": memories,
        "resources": resources,
        "skills": skills,
        "query_plan": result.get("query_plan"),
        "all_items": all_items,
        "all_items_raw": all_items_raw,
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

    scored = sorted(items, key=lambda x: float(x.get("score") or 0), reverse=True)

    # Backends without tiered loading (no abstract/overview) skip L0/L1
    tiered = getattr(backend, "supports_tiered_loading", True)
    if not tiered:
        # Direct L2: search results already have abstracts embedded; just read top items
        blocks = []
        used_uris = []
        for item in scored[: max_l2 if max_l2 > 0 else 2]:
            uri = item.get("uri", "")
            if not uri:
                continue
            try:
                content = backend.read(uri)
                if content and len(str(content)) > 20:
                    blocks.append(f"[SOURCE: {uri}]\n{str(content)[:1500]}")
                    used_uris.append(uri)
            except Exception as e:
                log.debug("direct read failed for %s: %s", uri, e)
        context_text = "\n\n".join(blocks)
        log.info(
            "context 加载: stage=direct_read (no tiered loading), sources=%d, chars=%d",
            len(used_uris),
            len(context_text),
        )
        return context_text, used_uris, "L2"

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
        except Exception as e:
            log.debug("L1 overview failed for %s: %s", uri, e)

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
    for item in scored[: max(3, max_l2)]:
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
        except Exception as e:
            log.debug("L2 read failed for %s: %s", uri, e)

    context_text = "\n\n".join(blocks)
    log.info(
        "context 加载: stage=L2 fallback, sources=%d, L2=%d, chars=%d", len(used_uris), l2_count, len(context_text)
    )
    return context_text, used_uris, "L2"


def _keyword_overlap(query: str, items: list) -> float:
    """Check how many query keywords appear in top item abstracts.

    Returns a ratio in [0.0, 1.0].  Higher = more keywords covered locally.
    """
    import re

    if not query or not items:
        return 0.0

    # Extract query keywords (EN tokens 3+ chars, CN tokens 2-4 chars)
    en_tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9_\-/.]{3,}", query)}
    cn_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,4}", query))
    keywords = en_tokens | cn_tokens
    if not keywords:
        return 1.0  # no extractable keywords = can't measure, assume OK

    # Combine abstracts from top items
    combined = " ".join(str(it.get("abstract", "")) for it in items[:5]).lower()

    matched = sum(1 for kw in keywords if kw.lower() in combined)
    return matched / len(keywords)


def assess_coverage(result: dict, query: str = "") -> tuple:
    """基于 OV 原始 score 评估覆盖率。

    使用 result["all_items_raw"]（OV 原始分，未经 feedback 调整），
    确保覆盖率判断和是否触发外搜只依赖 OV 自身信号，不受 feedback 影响。
    若 all_items_raw 不存在（旧调用兼容），退回到 all_items。

    信号组合（0 LLM 调用）：
    - OV score 阈值 + 结果数量（基础判断）
    - Score gap：top1 >> top2 表示可能是孤立命中，降低信心
    - Keyword overlap：query 关键词在 abstract 中的覆盖率

    返回: (coverage: float, need_external: bool, reason: str)
    """
    # 优先用原始分，保证外搜决策不受 feedback 影响
    all_items = result.get("all_items_raw") or result.get("all_items", [])

    if not all_items:
        return 0.0, True, "no_results"

    scored_items = [x for x in all_items if x.get("score", 0) > 0]
    if not scored_items:
        return 0.0, True, "no_scores"

    scores = sorted((float(x.get("score") or 0) for x in scored_items), reverse=True)
    avg_score = sum(scores) / len(scores)
    top_score = scores[0]
    count = len(scores)

    # ── Score gap penalty ──
    # If top1 >> top2 by a large margin, it's likely an isolated hit
    # (one lucky document, rest are noise).  Reduce effective coverage.
    gap_penalty = 0.0
    if count >= 2:
        gap = scores[0] - scores[1]
        if gap > _GAP_PENALTY_THRESHOLD:
            gap_penalty = min(_GAP_PENALTY_CAP, gap * _GAP_PENALTY_MULTIPLIER)

    # ── Keyword overlap ──
    kw_overlap = _keyword_overlap(query, scored_items)
    # Low keyword coverage = local results might not actually answer the query
    kw_penalty = 0.0
    if kw_overlap < _KW_OVERLAP_LOW:
        kw_penalty = _KW_PENALTY_LOW
    elif kw_overlap < _KW_OVERLAP_MED:
        kw_penalty = _KW_PENALTY_MED

    # ── Scale factor ──
    # 规模修正：知识库结果数少时，OV score 分布不可信，适当放宽阈值
    scale_factor = min(1.0, _SCALE_FACTOR_BASE + _SCALE_FACTOR_PER_ITEM * count)
    effective_sufficient = THRESHOLD_COV_SUFFICIENT * scale_factor
    effective_marginal = THRESHOLD_COV_MARGINAL * scale_factor

    # Apply penalties to effective score for threshold comparison
    adjusted_top = top_score - gap_penalty - kw_penalty

    # 基于有效阈值判断
    if adjusted_top > effective_sufficient and count >= 2:
        coverage = min(1.0, avg_score + _COV_BONUS_SUFFICIENT - gap_penalty)
        reason = "local_sufficient"
        need_external = False
    elif adjusted_top > effective_marginal and count >= 1:
        coverage = avg_score - gap_penalty * _COV_DISCOUNT_MARGINAL
        reason = "local_marginal"
        need_external = True
    elif adjusted_top > THRESHOLD_COV_LOW and count >= 1:
        coverage = avg_score * _COV_DISCOUNT_LOW
        reason = "low_coverage"
        need_external = True
    else:
        coverage = avg_score * _COV_DISCOUNT_INSUFFICIENT
        reason = "insufficient"
        need_external = True

    coverage = max(0.0, min(1.0, coverage))
    return coverage, need_external, reason
