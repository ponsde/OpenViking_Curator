"""Retrieval: local search, keyword index, priority context building."""

import asyncio
import json
import os
import re
from pathlib import Path

from .config import (
    log, _GENERIC_TERMS, FAST_ROUTE,
    THRESHOLD_CURATED_OVERLAP, THRESHOLD_CURATED_MIN_HITS,
)
from .feedback import (
    uri_feedback_score, uri_trust_score, uri_freshness_score,
    build_feedback_priority_uris, load_feedback,
)

def deterministic_relevance(query: str, scope: dict, txt: str, uris: list, domain_hit: bool, kw_cov: float):
    txt_l = (txt or "").lower()
    q_terms = [x for x in re.findall(r"[a-z0-9_\-]{3,}", query.lower()) if x not in {"what", "with", "from", "that"}]
    k_terms = [str(k).lower() for k in scope.get("keywords", [])[:8] if isinstance(k, str)]
    terms = list(dict.fromkeys(q_terms + k_terms))[:12]

    evidence_hit = sum(1 for t in terms if t and t in txt_l)
    evidence_ratio = evidence_hit / max(1, len(terms))

    uri_text = " ".join(uris).lower()
    scope_terms = [str(scope.get("domain", "")).lower()] + [str(x).lower() for x in scope.get("keywords", [])[:4]]
    uri_scope_hit = any(t and t in uri_text for t in scope_terms)

    relevance = 0.55 * kw_cov + 0.30 * evidence_ratio + 0.15 * (1.0 if (domain_hit or uri_scope_hit) else 0.0)
    return relevance, evidence_ratio, uri_scope_hit


def _local_index_search(query: str, kw_list: list, topn: int = 5) -> list:
    """用本地关键词索引兜底 OpenViking 检索的不稳定性"""
    idx_path = os.path.join(os.path.dirname(__file__), '.curated_index.json')
    if not os.path.exists(idx_path):
        return []
    try:
        import json
        idx = json.loads(open(idx_path).read())
    except Exception:
        return []
    ql = query.lower()
    q_terms = set(re.findall(r"[a-z0-9_\-]{2,}", ql)) | set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    q_terms.update(k.lower() for k in kw_list if k)
    scored = []
    for uri, info in idx.items():
        text = (info.get('title', '') + ' ' + info.get('preview', '')).lower()
        hits = sum(1 for t in q_terms if t in text)
        if hits > 0:
            scored.append((uri, hits, info.get('preview', '')[:1500]))
    scored.sort(key=lambda x: -x[1])
    return scored[:topn]


# ── 模块级常量：通用词（不作为相关性证据） ──
_GENERIC_TERMS = {
    "2.0", "3.0", "1.0", "0.1", "2025", "2026", "2024", "最新", "latest",
    "对比", "比较", "区别", "最佳", "实践", "方案", "选型", "推荐",
    "怎么", "如何", "什么", "为什么", "哪些", "入门", "指南",
    "compare", "best", "practice", "guide", "tutorial", "how",
    "vs", "versus", "performance", "benchmark",
}


def _intent_search(client, query: str, limit: int = 5) -> list:
    """用 OpenViking 原生 IntentAnalyzer 做智能检索。
    
    VLM 分析 query 意图，拆成多个 TypedQuery（resource/memory/skill），
    分别检索后合并去重。比手写关键词展开更准确。
    
    失败时静默返回空列表，不影响主流程。
    """
    try:
        from openviking.retrieve.intent_analyzer import IntentAnalyzer

        analyzer = IntentAnalyzer(max_recent_messages=5)

        async def _analyze():
            return await analyzer.analyze(
                compression_summary="",
                messages=[],
                current_message=query,
            )

        # 在新 event loop 里跑，避免跟 sync 环境冲突
        plan = asyncio.run(_analyze())
        
        all_items = []
        seen_uris = set()
        
        for tq in plan.queries[:5]:  # 最多取 5 个子查询
            try:
                res = client.search(tq.query, limit=limit)
                for x in (getattr(res, "resources", []) or []):
                    u = getattr(x, "uri", "")
                    if u and u not in seen_uris:
                        seen_uris.add(u)
                        all_items.append(x)
            except Exception:
                pass
        
        log.info("IntentAnalyzer: %d 子查询 → %d 个结果", len(plan.queries), len(all_items))
        return all_items
        
    except Exception as e:
        log.debug("IntentAnalyzer 失败，回退普通检索: %s", e)
        return []


def local_search(client, query: str, scope: dict):
    # 缩写展开：短缩写在语义检索中容易被淹没，展开全称提升召回
    _ABBR_MAP = {
        "mcp": "MCP Model Context Protocol",
        "rag": "RAG Retrieval-Augmented Generation",
        "k8s": "Kubernetes K8s",
        "ci/cd": "CI/CD Continuous Integration Continuous Deployment",
        "llm": "LLM Large Language Model",
        "vlm": "VLM Vision Language Model",
        "oom": "OOM Out Of Memory OOMKilled",
    }
    expanded_q = query
    ql = query.lower()
    for abbr, full in _ABBR_MAP.items():
        if abbr in ql:
            expanded_q = f"{query} {full}"
            break

    expanded = expanded_q + "\n关键词:" + ",".join(scope.get("keywords", [])[:8])

    # 双路检索：find() 语义更精准，search() 覆盖更广，取并集
    # 多轮检索对冲 OpenViking 向量检索的随机性
    all_items = []
    seen_uris = set()
    search_queries = [expanded]
    if expanded_q != query:
        search_queries.append(expanded_q)  # 缩写全称版

    # 快速模式：只用 search()（纯向量，不走 LLM query planning）
    # find() 慢但更精准，仅在需要时用一次
    methods = [client.search]
    if not FAST_ROUTE:
        methods.insert(0, client.find)

    for sq in search_queries:
        for method in methods:
            try:
                res = method(sq)
                for x in (getattr(res, "resources", []) or []):
                    u = getattr(x, "uri", "")
                    if u and u not in seen_uris:
                        seen_uris.add(u)
                        all_items.append(x)
            except Exception:
                pass

    txt = str(all_items[:5])

    # ── OV 原生智能检索（IntentAnalyzer）──
    # 用 VLM 分析意图，拆成多角度子查询，补充普通检索可能遗漏的结果
    intent_items = _intent_search(client, query, limit=5)
    for x in intent_items:
        u = getattr(x, "uri", "")
        if u and u not in seen_uris:
            seen_uris.add(u)
            all_items.append(x)

    # ── 本地索引兜底 ──
    # OpenViking 检索不稳定时，用关键词索引补充候选
    idx_hits = _local_index_search(query, scope.get("keywords", []))
    idx_uris_added = set()
    for idx_uri, _, idx_preview in idx_hits:
        if idx_uri not in seen_uris:
            seen_uris.add(idx_uri)
            idx_uris_added.add(idx_uri)
            # 创建一个简易 mock 对象
            class _MockResult:
                def __init__(self, u, p):
                    self.uri = u; self.abstract = ''; self._preview = p
            all_items.append(_MockResult(idx_uri, idx_preview))

    # 过滤噪声
    NOISE_PATTERNS = ("viking://resources/tmp", "/tmp", "tmpr", "快速上手",
                      "许可证", "核心理念", "前置要求", "/document/content")
    def _is_noise(u: str) -> bool:
        ul = (u or "").lower()
        return any(p in ul for p in NOISE_PATTERNS)

    items = [x for x in all_items
             if str(getattr(x, "uri", "")).startswith("viking://resources")
             and not _is_noise(str(getattr(x, "uri", "")))]

    uris = [getattr(x, "uri", "") for x in items]
    abstracts = [getattr(x, "abstract", "") or "" for x in items]

    # ── 构建关键词列表 ──
    kw = [str(k).strip().lower() for k in scope.get("keywords", [])[:6]
          if isinstance(k, str) and str(k).strip()]
    q_tokens = re.findall(r"[a-z0-9_\-]{2,}", query.lower())
    kw.extend(q_tokens[:6])
    ql = query.lower()

    # ── 核心词 vs 通用词区分 ──
    core_kw = [k for k in kw if k.lower() not in _GENERIC_TERMS and len(k) >= 2]
    generic_kw = [k for k in kw if k.lower() in _GENERIC_TERMS]

    # 手工锚点（高频内部术语）
    _anchors = {
        "newapi": ["newapi", "oneapi", "openai", "api gateway"],
        "oneapi": ["newapi", "oneapi", "openai"],
        "mcp": ["mcp", "model context protocol", "tool server"],
        "nginx": ["nginx", "reverse proxy", "upstream", "502", "bad gateway"],
        "docker": ["docker", "container", "dockerfile"],
        "git": ["git", "rebase", "cherry-pick", "reflog"],
        "openviking": ["openviking", "viking", "agfs", "contextual filesystem"],
        "grok2api": ["grok2api", "grok", "auto register", "curated"],
        "asyncio": ["asyncio", "coroutine", "event loop", "await"],
        "github actions": ["github actions", "ci/cd", "workflow", "yaml"],
        "rag": ["rag", "retrieval", "chunk", "rerank", "embedding"],
        "kubernetes": ["kubernetes", "k8s", "pod", "crashloopbackoff"],
        "systemd": ["systemd", "systemctl", "service", "unit file"],
        "claude": ["claude", "anthropic", "openai", "api compatibility"],
        "向量数据库": ["vector database", "milvus", "chroma", "qdrant", "weaviate"],
    }
    for anchor_key, anchor_terms in _anchors.items():
        if anchor_key in ql:
            kw.extend(anchor_terms)
    kw = list(dict.fromkeys([k for k in kw if k]))[:16]

    # ── 构建相关性文本 ──
    # URI + 摘要 + top 资源正文预览（abstract 可能为空，所以正文是核心信号）
    previews = []
    for x in items[:5]:
        u = getattr(x, 'uri', '')
        # 优先用索引缓存的 preview，其次 client.read()
        if hasattr(x, '_preview') and x._preview:
            previews.append(x._preview)
        else:
            try:
                content = str(client.read(u))[:1500]
                previews.append(content)
            except Exception:
                pass
    # abstract 为空时完全依赖正文
    relevance_text = ("\n".join(uris[:8]) + "\n" + "\n".join(abstracts[:5])
                      + "\n" + "\n".join(previews)).lower()

    hit = sum(1 for k in kw if k in relevance_text)
    kw_cov = hit / max(1, len(kw))

    # ── 核心词覆盖率（更准确的相关性信号） ──
    # 对短词（<=4字符）用词边界匹配，避免 "bun" 命中 "ubuntu" 等
    def _core_match(term, text):
        if len(term) <= 4:
            return bool(re.search(r'(?<![a-z])' + re.escape(term) + r'(?![a-z])', text))
        return term in text

    core_hit = sum(1 for k in core_kw if _core_match(k, relevance_text))
    core_cov = core_hit / max(1, len(core_kw)) if core_kw else kw_cov

    # 语义连贯性检查：如果核心词覆盖低但通用词拉高了 kw_cov，惩罚
    if core_kw and core_cov < 0.3 and kw_cov > 0.5:
        kw_cov = kw_cov * 0.3  # 严重惩罚：核心词几乎没命中

    # ── 领域词命中 ──
    target_terms = []
    for anchor_key, anchor_terms in _anchors.items():
        if anchor_key in ql:
            target_terms.extend(anchor_terms)
    target_terms = list(dict.fromkeys(target_terms))

    full_text = (" ".join(uris) + " " + " ".join(abstracts) + " " + " ".join(previews)).lower()
    domain_hit = any(t in full_text for t in target_terms) if target_terms else False

    relevance, evidence_ratio, uri_scope_hit = deterministic_relevance(
        query, scope, relevance_text, uris, domain_hit, kw_cov)

    # ── coverage 计算 ──
    effective_domain_hit = (domain_hit
                           or (uri_scope_hit and evidence_ratio >= 0.2)
                           or (relevance >= 0.55 and core_cov >= 0.3))

    # 噪声惩罚：证据弱但关键词覆盖高
    if evidence_ratio < 0.15 and kw_cov > 0.5:
        kw_cov = kw_cov * 0.35

    # 核心词缺失惩罚：即使通用词命中多，核心词没命中就不算真覆盖
    if core_kw and core_cov < 0.2:
        coverage = min(max(kw_cov, relevance), 0.25) if effective_domain_hit else min(max(kw_cov, relevance), 0.10)
    else:
        coverage = max(kw_cov, relevance) if effective_domain_hit else min(max(kw_cov, relevance), 0.18)

    # curated 资源加权：搜到我们入库过的文档说明知识库里有相关内容
    def _is_our_doc(u):
        ul = u.lower()
        return any(tag in ul for tag in ("curated", "single_", "reingest_", "fix_", "re2_"))
    curated_uris = [u for u in uris if _is_our_doc(u)]
    if curated_uris:
        # 用 query 核心英文词（去掉通用词）在正文中匹配
        core_en = set(re.findall(r"[a-zA-Z0-9_\-]{3,}", query.lower())) - _GENERIC_TERMS
        core_cn = set(re.findall(r"[\u4e00-\u9fff]{3,4}", query)) - _GENERIC_TERMS
        query_terms = core_en | core_cn
        preview_text = " ".join(previews).lower()
        content_overlap = sum(1 for t in query_terms if t and t.lower() in preview_text)
        overlap_ratio = content_overlap / max(1, len(query_terms))
        if overlap_ratio >= THRESHOLD_CURATED_OVERLAP or content_overlap >= THRESHOLD_CURATED_MIN_HITS:
            curated_bonus = 0.10 * min(len(curated_uris), 3)
            coverage = max(coverage, 0.40) + curated_bonus
            coverage = min(1.0, coverage)

    # 本地索引强兜底：如果索引命中了高相关文档但 OpenViking 检索随机性导致 coverage 低
    if coverage < 0.45:
        idx_results = _local_index_search(query, kw)
        if idx_results:
            best_hits = idx_results[0][1]
            best_preview = idx_results[0][2].lower()
            # 至少 3 个关键词命中才算强匹配
            if best_hits >= 3:
                idx_terms = set(kw) | set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
                idx_overlap = sum(1 for t in idx_terms if t and t.lower() in best_preview)
                if idx_overlap >= 2:
                    coverage = max(coverage, 0.50)

    # feedback 调权
    fb = load_feedback(os.getenv('CURATOR_FEEDBACK_FILE', 'feedback.json'))
    uri_scores = {u: uri_feedback_score(u, fb) for u in uris[:20]}
    max_fb = max(uri_scores.values()) if uri_scores else 0
    if max_fb > 0:
        coverage = min(1.0, coverage + 0.08 * max_fb)

    pri_uris, rank_preview = build_feedback_priority_uris(
        uris, os.getenv('CURATOR_FEEDBACK_FILE', 'feedback.json'), topn=3)

    top_trust = [x[2] for x in rank_preview[:3]] if rank_preview else []
    avg_top_trust = (sum(top_trust) / len(top_trust)) if top_trust else 0.0
    fresh_ratio = (len(curated_uris) / max(1, min(8, len(uris)))) if uris else 0.0

    return txt, coverage, {
        "kw_cov": round(kw_cov, 3),
        "core_cov": round(core_cov, 3),
        "domain_hit": effective_domain_hit,
        "target_terms": target_terms[:6],
        "uris": uris[:8],
        "max_feedback_score": max_fb,
        "priority_uris": pri_uris,
        "rank_preview": rank_preview,
        "relevance": round(relevance, 3),
        "evidence_ratio": round(evidence_ratio, 3),
        "uri_scope_hit": uri_scope_hit,
        "avg_top_trust": round(avg_top_trust, 3),
        "fresh_ratio": round(fresh_ratio, 3),
    }




def build_priority_context(client, uris, query: str = ""):
    """读取优先资源内容。如果提供 query，用核心词验证相关性，过滤不相关文档。"""
    blocks = []
    # 核心词验证：如果提供了 query，只保留内容中包含核心词的文档
    if query:
        q_core = set(re.findall(r"[a-zA-Z0-9_\-]{3,}", query.lower())) - _GENERIC_TERMS
        q_cn = set(re.findall(r"[\u4e00-\u9fff]{2,4}", query))
        check_terms = q_core | q_cn
    else:
        check_terms = set()

    for u in uris[:4]:  # 多看几个，过滤后可能不够
        try:
            c = str(client.read(u))[:1500]
            # 语义过滤：核心词至少命中1个才算相关
            if check_terms:
                c_lower = c.lower()
                hits = sum(1 for t in check_terms if t.lower() in c_lower)
                if hits == 0:
                    continue
            blocks.append(f"[PRIORITY_SOURCE] {u}\n{c[:1200]}")
            if len(blocks) >= 2:
                break
        except Exception:
            continue
    return "\n\n".join(blocks)


