#!/usr/bin/env python3
import os
import json
import re
import time
from pathlib import Path

import requests
import openviking as ov
from metrics import Metrics
from memory_capture import capture_case

"""
OpenViking Curator v0 (pilot)

Security:
- NO hardcoded API keys
- All secrets loaded from environment variables
"""


def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


# ---- Config from env ----
OPENVIKING_CONFIG_FILE = env("OPENVIKING_CONFIG_FILE", str(Path.home() / ".openviking" / "ov.conf"))
DATA_PATH = env("CURATOR_DATA_PATH", str(Path.cwd() / "data"))
CURATED_DIR = env("CURATOR_CURATED_DIR", str(Path.cwd() / "curated"))

OAI_BASE = env("CURATOR_OAI_BASE")  # e.g. https://oai.whidsm.cn/v1
OAI_KEY = env("CURATOR_OAI_KEY")

ROUTER_MODELS = [
    m.strip() for m in env(
        "CURATOR_ROUTER_MODELS",
        "gemini-3-flash-preview,gemini-3-flash-high,【Claude Code】Claude-Sonnet 4-5",
    ).split(",") if m.strip()
]
JUDGE_MODEL = env("CURATOR_JUDGE_MODEL", "gemini-3-flash-preview")
JUDGE_MODELS = [
    m.strip() for m in env("CURATOR_JUDGE_MODELS", "gemini-3-flash-preview,gemini-3-flash-high,【Claude Code】Claude-Sonnet 4-5").split(",") if m.strip()
]
ANSWER_MODELS = [
    m.strip() for m in env("CURATOR_ANSWER_MODELS", "gemini-3-flash-preview,gemini-3-flash-high,【Claude Code】Claude-Sonnet 4-5").split(",") if m.strip()
]

GROK_BASE = env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
GROK_KEY = env("CURATOR_GROK_KEY")
GROK_MODEL = env("CURATOR_GROK_MODEL", "grok-4-fast")


def validate_config() -> None:
    missing = []
    if not OAI_BASE:
        missing.append("CURATOR_OAI_BASE")
    if not OAI_KEY:
        missing.append("CURATOR_OAI_KEY")
    if not GROK_KEY:
        missing.append("CURATOR_GROK_KEY")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def chat(base, key, model, messages, timeout=60):
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _rule_based_scope(query: str) -> dict:
    """纯规则路由：0 API 调用，<1ms 完成"""
    ql = query.lower()

    # ── 领域判定 ──
    _DOMAIN_MAP = {
        "technology": ["docker", "nginx", "linux", "k8s", "kubernetes", "systemd", "git",
                       "python", "asyncio", "rust", "golang", "javascript", "typescript",
                       "api", "mcp", "rag", "llm", "openai", "claude", "grok", "embedding",
                       "vector", "milvus", "chroma", "qdrant", "ci/cd", "github actions",
                       "terraform", "ansible", "openviking", "newapi", "oneapi", "grok2api",
                       "wordpress", "tailscale", "cloudflare", "向量", "容器", "反向代理",
                       "部署", "配置", "排查", "服务器", "数据库"],
        "devops": ["vps", "ssh", "firewall", "防火墙", "安全加固", "监控", "日志",
                   "systemctl", "journalctl", "iptables", "ufw"],
    }
    domain = "general"
    for d, terms in _DOMAIN_MAP.items():
        if any(t in ql for t in terms):
            domain = d
            break

    # ── 关键词提取 ──
    # 英文技术词
    en_tokens = re.findall(r"[a-zA-Z0-9_\-/.]{2,}", query)
    # 中文词切分（简易词典 + 字符 n-gram 兜底）
    _CN_TERMS = {
        "所有权", "模型", "理解", "排查", "配置", "注册", "入门", "对比", "选型",
        "安全", "加固", "防火墙", "日志", "网络", "存储", "容器", "反向代理",
        "常见问题", "最佳实践", "工作原理", "使用场景", "设计理念", "快速上手",
        "自动更新", "兼容性", "参数差异", "注意事项", "网关对比", "状态管理",
        "上下文", "文件系统", "向量数据库", "陷阱",
    }
    cn_tokens = []
    remaining = re.sub(r"[^\u4e00-\u9fff]", "", query)
    while remaining:
        matched = False
        for length in (4, 3, 2):
            if len(remaining) >= length and remaining[:length] in _CN_TERMS:
                cn_tokens.append(remaining[:length])
                remaining = remaining[length:]
                matched = True
                break
        if not matched:
            # 跳过单字
            remaining = remaining[1:]
    # 补充 regex 2-gram 防漏（但只保留在词典里或有意义的）
    bigrams = re.findall(r"[\u4e00-\u9fff]{2}", query)
    for bg in bigrams:
        if bg in _CN_TERMS and bg not in cn_tokens:
            cn_tokens.append(bg)
    cn_tokens = list(dict.fromkeys(cn_tokens))

    # 去掉停用词
    _STOP = {"是什么", "怎么", "如何", "什么", "哪些", "常见", "有哪些", "最佳", "实践",
             "怎么样", "可以", "应该", "为什么", "到底", "一下", "这个", "那个",
             "the", "what", "how", "is", "are", "and", "for", "with", "to", "in", "of"}
    keywords = [t for t in (en_tokens + cn_tokens) if t.lower() not in _STOP and len(t) > 1]
    # 去重保序
    keywords = list(dict.fromkeys(keywords))[:8]

    # ── 时效性判定 ──
    need_fresh = any(k in ql for k in ["最新", "更新", "release", "changelog", "2026", "2025", "latest"])

    return {
        "domain": domain,
        "keywords": keywords,
        "exclude": [],
        "need_fresh": need_fresh,
        "source_pref": ["official_docs", "tech_blog", "github"],
        "confidence": 0.7,
    }


# 环境变量控制：CURATOR_FAST_ROUTE=1 用规则（默认），=0 用 LLM
FAST_ROUTE = env("CURATOR_FAST_ROUTE", "1") == "1"


def route_scope(query: str):
    if FAST_ROUTE:
        return _rule_based_scope(query)

    # LLM fallback（慢但更智能）
    sys = (
        "你是检索路由器。把用户问题转换为严格JSON，字段: "
        "domain(字符串), keywords(数组), exclude(数组), need_fresh(boolean), source_pref(数组), confidence(0-1)。"
        "只输出JSON，不要解释。"
    )
    last_err = None
    out = None
    chosen = None
    for m in ROUTER_MODELS:
        try:
            out = chat(OAI_BASE, OAI_KEY, m, [
                {"role": "system", "content": sys},
                {"role": "user", "content": query},
            ], timeout=45)
            chosen = m
            break
        except Exception as e:
            last_err = e
            continue
    if out is None:
        raise RuntimeError(f"all router models failed: {last_err}")
    print(f"router_model_used={chosen}")

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {
            "domain": "general",
            "keywords": [query],
            "exclude": [],
            "need_fresh": True,
            "source_pref": ["official docs", "github"],
            "confidence": 0.5,
        }
    try:
        return json.loads(m.group(0))
    except Exception:
        return {
            "domain": "general",
            "keywords": [query],
            "exclude": [],
            "need_fresh": True,
            "source_pref": ["official docs", "github"],
            "confidence": 0.5,
        }


def load_feedback(path: str):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def uri_feedback_score(uri: str, fb: dict) -> int:
    if not isinstance(fb, dict):
        return 0

    def _score(item):
        up = int(item.get('up', 0))
        down = int(item.get('down', 0))
        adopt = int(item.get('adopt', 0))
        return up - down + adopt * 2

    # exact match
    if uri in fb:
        return _score(fb[uri])

    # fuzzy match: same subtree / parent-child path overlap
    best = 0
    for k, v in fb.items():
        if not isinstance(k, str):
            continue
        if k in uri or uri in k:
            best = max(best, _score(v))
    return best


def uri_trust_score(uri: str) -> float:
    u = (uri or '').lower()
    s = 5.0
    if 'openviking' in u or 'grok2api' in u or 'newapi' in u:
        s += 1.0
    if 'curated' in u:
        s += 0.5
    if 'license' in u:
        s -= 0.5
    return s


def uri_freshness_score(uri: str) -> float:
    # very light heuristic: curated entries likely newer
    u = (uri or '').lower()
    return 1.0 if 'curated' in u else 0.0


def build_feedback_priority_uris(uris, feedback_file='feedback.json', topn=3):
    fb = load_feedback(feedback_file)
    scored = []
    seen = set()
    for u in uris:
        if u in seen:
            continue
        seen.add(u)
        f = uri_feedback_score(u, fb)             # strong user signal
        t = uri_trust_score(u)                    # weak prior
        r = uri_freshness_score(u)                # freshness prior
        final = 0.50 * f + 0.30 * t + 0.20 * r
        scored.append((final, f, t, r, u))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [x[4] for x in scored[:topn]], scored[:min(5, len(scored))]


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
    # 通用词：出现在大量不同主题文档中，不能作为相关性证据
    _GENERIC_TERMS = {
        "2.0", "3.0", "1.0", "0.1", "2025", "2026", "2024", "最新", "latest",
        "对比", "比较", "区别", "最佳", "实践", "方案", "选型", "推荐",
        "怎么", "如何", "什么", "为什么", "哪些", "入门", "指南",
        "compare", "best", "practice", "guide", "tutorial", "how",
        "vs", "versus", "performance", "benchmark",
    }
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
        if overlap_ratio >= 0.25 or content_overlap >= 3:
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


def external_boost_needed(query: str, scope: dict, coverage: float, meta: dict):
    q = (query or "").lower()
    need_fresh = bool(scope.get("need_fresh", False)) or any(k in q for k in ["最新", "更新", "release", "changelog", "2026", "2025"])
    low_quality = meta.get("avg_top_trust", 0) < 5.4
    low_fresh = meta.get("fresh_ratio", 0) < 0.25
    weak_feedback = meta.get("max_feedback_score", 0) <= 0
    core_cov = meta.get("core_cov", 1.0)

    # 覆盖率阈值（已知内部域名可更宽松，减少重复外搜）
    low_cov_threshold = 0.45
    if any(k in q for k in ["newapi", "openviking", "grok2api", "mcp"]):
        low_cov_threshold = 0.35

    if coverage < low_cov_threshold:
        return True, "low_coverage"
    # 核心词覆盖低 = 知识库对这个话题实际没覆盖，即使通用词拉高了 coverage
    if core_cov <= 0.4:
        return True, "low_core_coverage"
    if need_fresh and (low_fresh or low_quality):
        return True, "freshness_or_quality_boost"
    if need_fresh and weak_feedback and low_quality:
        return True, "need_fresh_no_positive_feedback"
    return False, "local_sufficient"


def external_search(query: str, scope: dict):
    import datetime
    today = datetime.date.today().isoformat()
    prompt = (
        f"问题: {query}\n"
        f"关键词: {scope.get('keywords', [])}\n"
        f"排除: {scope.get('exclude', [])}\n"
        f"偏好来源: {scope.get('source_pref', [])}\n"
        f"当前日期: {today}\n\n"
        "要求:\n"
        "1. 返回5条高质量来源，格式：标题+URL+发布/更新日期+关键点\n"
        "2. 优先最近6个月内的信息，标注每条来源的日期\n"
        "3. 如果引用的项目/文档超过1年未更新，明确标注[可能过时]\n"
        "4. 涉及API、注册流程、认证方式等易变内容时，必须确认当前是否仍然有效\n"
        "5. 不要把旧版本的技术要求当成当前事实（如已取消的验证步骤）\n"
        "6. GitHub项目必须标注：最后commit日期、star数、是否archived\n"
        "7. 区分[可直接使用]和[仅供参考]——维护中且有文档的才算可用"
    )
    return chat(GROK_BASE, GROK_KEY, GROK_MODEL, [
        {"role": "system", "content": (
            "你是实时搜索助手。重视可验证来源和信息时效性。"
            f"当前日期: {today}。"
            "对于技术类问题，优先引用官方文档和近期更新。"
            "如果搜到的信息可能已过时（如超过1年的项目、已变更的API流程），"
            "必须明确标注并提示用户验证。"
            "对于GitHub项目，务必区分：项目存在 ≠ 项目能用。"
        )},
        {"role": "user", "content": prompt},
    ], timeout=90)


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
                print(f"  ⚠️ cross_validate model {cv_model} failed: {e}")
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
            print(f"  🔄 交叉验证: 追问 → {result['followup_query']}")
            try:
                followup_text = chat(GROK_BASE, GROK_KEY, GROK_MODEL, [
                    {"role": "system", "content": (
                        f"你是实时搜索助手。当前日期: {today}。"
                        "请搜索最新官方信息来验证以下声明是否仍然成立。"
                        "优先引用官方文档、Help Center、Release Notes。"
                    )},
                    {"role": "user", "content": (
                        f"需要验证的声明:\n" +
                        "\n".join([f"- {c.get('claim','')}" for c in high_risk]) +
                        f"\n\n验证搜索: {result['followup_query']}"
                    )},
                ], timeout=60)
                print(f"  ✅ 追问完成: {len(followup_text)} chars")
            except Exception as e:
                print(f"  ⚠️ 追问失败: {e}")

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
        print(f"  ⚠️ 交叉验证异常: {e}")
        return {"validated": external_text, "warnings": [], "followup_done": False}


def judge_and_pack(query: str, external_text: str):
    import datetime
    today = datetime.date.today().isoformat()
    sys = (
        "你是资料审核器。判断外部搜索结果是否值得入库。\n"
        f"当前日期: {today}\n\n"
        "审核维度:\n"
        "1. 内容准确性 — 信息是否正确、是否有来源支撑\n"
        "2. 时效性 — 信息是否仍然有效？API流程/注册方式/技术要求等易变内容尤其注意\n"
        "   - 超过1年未更新的项目信息：trust降低，标注[可能过时]\n"
        "   - 引用已取消/变更的功能当作当前事实：pass=false\n"
        "   - 将旧版本要求（如已取消的手机验证）当成现行要求：pass=false\n"
        "3. 入库价值 — 是否值得长期保存，还是只是临时参考\n\n"
        "输出严格JSON: pass(bool), reason(string), tags(array), trust(0-10), "
        "freshness(string: current/recent/outdated/unknown), "
        "summary(string), markdown(string)。\n"
        "markdown要求包含来源URL和信息日期。只输出JSON。"
    )

    last_err = None
    out = None
    for jm in JUDGE_MODELS:
        try:
            print(f"judge_model_used={jm}")
            out = chat(OAI_BASE, OAI_KEY, jm, [
                {"role": "system", "content": sys},
                {"role": "user", "content": f"用户问题:{query}\n候选资料:\n{external_text}"},
            ], timeout=90)
            break
        except Exception as e:
            last_err = e
            continue

    if out is None:
        return {"pass": False, "reason": f"judge_model_fail:{last_err}", "tags": [], "trust": 0, "summary": "", "markdown": ""}

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"pass": False, "reason": "bad_json", "tags": [], "trust": 0, "summary": "", "markdown": ""}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"pass": False, "reason": "json_parse_fail", "tags": [], "trust": 0, "summary": "", "markdown": ""}


def ingest_markdown(client, title: str, markdown: str, freshness: str = "unknown"):
    import datetime
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)

    # P2: 入库时写入 metadata（日期 + 时效标签）
    today = datetime.date.today().isoformat()
    ttl_map = {"current": 180, "recent": 90, "unknown": 60, "outdated": 0}
    ttl_days = ttl_map.get(freshness, 60)

    header = (
        f"<!-- curator_meta: ingested={today} freshness={freshness} ttl_days={ttl_days} -->\n"
        f"<!-- review_after: {(datetime.date.today() + datetime.timedelta(days=ttl_days)).isoformat()} -->\n\n"
    )

    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(header + markdown, encoding="utf-8")
    ing = client.add_resource(path=str(fn))

    # 关键修复：入库后等待语义索引完成，否则下一次检索拿不到新文档
    try:
        uri = ing.get("root_uri", "") if isinstance(ing, dict) else ""
        if uri:
            client.wait_processed()  # 不传参：等全部队列完成
    except Exception:
        pass

    return ing


def build_priority_context(client, uris):
    blocks = []
    for u in uris[:2]:
        try:
            c = client.read(u)
            blocks.append(f"[PRIORITY_SOURCE] {u}\n{str(c)[:1200]}")
        except Exception:
            continue
    return "\n\n".join(blocks)


def detect_conflict(query: str, local_ctx: str, external_ctx: str):
    if not external_ctx.strip():
        return {"has_conflict": False, "summary": "", "points": []}

    sys = (
        "你是冲突检测器。比较本地上下文与外部补充是否存在结论冲突。"
        "输出严格JSON：has_conflict(bool), summary(string), points(array of string)。"
        "如果只是细节差异但不影响结论，has_conflict=false。只输出JSON。"
    )
    out = chat(OAI_BASE, OAI_KEY, JUDGE_MODEL, [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"问题:{query}\n\n本地:\n{local_ctx[:2500]}\n\n外部:\n{external_ctx[:2500]}"},
    ], timeout=60)
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"has_conflict": False, "summary": "", "points": []}
    try:
        j = json.loads(m.group(0))
        if "points" not in j or not isinstance(j.get("points"), list):
            j["points"] = []
        return j
    except Exception:
        return {"has_conflict": False, "summary": "", "points": []}


def answer(query: str, local_ctx: str, external_ctx: str, priority_ctx: str = "",
           conflict_card: str = "", warnings: list = None):
    import datetime
    today = datetime.date.today().isoformat()
    warning_block = ""
    if warnings:
        warning_block = "\n⚠️ 以下信息需谨慎对待（可能过时或未经验证）:\n" + "\n".join([f"- {w}" for w in warnings[:5]])

    sys = (
        f"你是技术助手。当前日期: {today}。基于给定上下文回答。\n"
        "规则:\n"
        "1. 最后给来源列表，标注每个来源的日期\n"
        "2. 若存在冲突卡片，先展示冲突再给建议\n"
        "3. 对于不确定的信息，明确标注「⚠️ 待验证」\n"
        "4. 引用超过1年的资料时，提醒可能过时\n"
        "5. 区分「经过验证的事实」和「来自第三方项目的实现细节」\n"
        "6. 如果有警告信息，在回答开头提示用户注意"
    )
    user = (
        f"问题:\n{query}\n\n"
        f"{warning_block}\n\n"
        f"冲突卡片:\n{conflict_card}\n\n"
        f"优先来源上下文:\n{priority_ctx[:2500]}\n\n"
        f"本地上下文:\n{local_ctx[:5000]}\n\n"
        f"外部补充:\n{external_ctx[:3000]}"
    )

    last_err = None
    for m in ANSWER_MODELS:
        try:
            print(f"answer_model_used={m}")
            return chat(OAI_BASE, OAI_KEY, m, [
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ], timeout=90)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"all answer models failed: {last_err}")


def _build_source_footer(meta: dict, coverage: float, external_used: bool,
                         warnings: list = None) -> str:
    """生成回答底部的来源透明度信息"""
    lines = ["---", "📊 **回答质量信息**"]

    # 覆盖率
    cov_pct = int(coverage * 100)
    if cov_pct >= 80:
        cov_label = "✅ 高"
    elif cov_pct >= 50:
        cov_label = "⚠️ 中等"
    else:
        cov_label = "❌ 低"
    lines.append(f"- 知识库覆盖率: {cov_pct}% ({cov_label})")
    lines.append(f"- 核心词覆盖: {meta.get('core_cov', '?')}")

    # 来源
    if external_used:
        lines.append("- 来源: 本地知识库 + 外部搜索（已交叉验证）")
    else:
        lines.append("- 来源: 本地知识库")

    # 使用的资源
    uris = meta.get('priority_uris', [])
    if uris:
        short_uris = [u.split('/')[-1].replace('.md', '') for u in uris[:3]]
        lines.append(f"- 主要参考: {', '.join(short_uris)}")

    # 警告
    if warnings:
        lines.append(f"- ⚠️ 有 {len(warnings)} 条待验证信息")

    return "\n".join(lines)


def run(query: str):
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    print("STEP 1/8 初始化...")
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()
    m.step('init', True)
    print("✅ STEP 1 完成")

    try:
        print("STEP 2/8 范围路由...")
        scope = route_scope(query)
        m.step('route', True, {'domain': scope.get('domain'), 'confidence': scope.get('confidence')})
        m.score('router_confidence', scope.get('confidence', 0))
        print("✅ STEP 2 完成:", json.dumps(scope, ensure_ascii=False))

        print("STEP 3/8 本地检索(OpenViking)...")
        local_txt, coverage, meta = local_search(client, query, scope)
        m.step('local_search', True, {'coverage': coverage, 'kw_cov': meta.get('kw_cov'), 'domain_hit': meta.get('domain_hit')})
        m.score('coverage_before_external', round(coverage, 3))
        print(
            f"✅ STEP 3 完成: coverage={coverage:.2f}, kw_cov={meta['kw_cov']:.2f}, core_cov={meta.get('core_cov', '?')}, "
            f"domain_hit={meta['domain_hit']}, relevance={meta.get('relevance')}, evidence={meta.get('evidence_ratio')}, "
            f"avg_trust={meta.get('avg_top_trust')}, fresh_ratio={meta.get('fresh_ratio')}, fb_max={meta.get('max_feedback_score',0)}, "
            f"priority_uris={meta.get('priority_uris',[])}, rank_preview={meta.get('rank_preview',[])}, "
            f"target_terms={meta['target_terms']}, uris={meta.get('uris', [])}"
        )

        external_txt = ""
        ingested = False
        boost_needed, boost_reason = external_boost_needed(query, scope, coverage, meta)
        if boost_needed:
            m.flag('external_triggered', True)
            m.flag('external_reason', boost_reason)
            print(f"STEP 4/8 触发外部搜索(Grok)... reason={boost_reason}")
            external_txt = external_search(query, scope)
            m.step('external_search', True, {'len': len(external_txt), 'reason': boost_reason})
            print("✅ STEP 4 完成: 外部结果长度", len(external_txt))

            print("STEP 5/8 交叉验证...")
            cv = cross_validate(query, external_txt, scope)
            external_txt = cv.get("validated", external_txt)
            cv_warnings = cv.get("warnings", [])
            m.step('cross_validate', True, {
                'followup_done': cv.get('followup_done', False),
                'high_risk_count': cv.get('high_risk_count', 0),
                'warnings': cv_warnings[:3],
            })
            if cv_warnings:
                print(f"  ⚠️ 交叉验证警告: {cv_warnings}")
            else:
                print("  ✅ 无高风险声明")

            print("STEP 6/8 审核并尝试入库...")
            j = judge_and_pack(query, external_txt)
            m.step('judge', True, {'pass': j.get('pass'), 'trust': j.get('trust')})
            print("审核结果:", json.dumps({k: j.get(k) for k in ["pass", "reason", "trust", "tags", "freshness"]}, ensure_ascii=False))
            if j.get("pass") and j.get("markdown"):
                # 时效性拦截：outdated 的信息不入库
                freshness = j.get("freshness", "unknown")
                if freshness == "outdated":
                    m.step('ingest', False, {'reason': 'outdated_info'})
                    print("⚠️ 未入库: 信息已过时 (freshness=outdated)")
                else:
                    ing = ingest_markdown(client, "curated", j["markdown"], freshness=freshness)
                    ingested = True
                    m.step('ingest', True, {'uri': ing.get('root_uri', '')})
                    print("✅ 已入库:", ing.get("root_uri", ""))
            else:
                m.step('ingest', False)
                print("⚠️ 未入库")
        else:
            m.flag('external_triggered', False)
            m.flag('external_reason', boost_reason)
            cv_warnings = []
            print("STEP 4/8 跳过外部搜索（本地覆盖与质量足够）")

        print("STEP 7/8 冲突检测...")
        conflict = detect_conflict(query, local_txt, external_txt)
        conflict_card = ""
        if conflict.get('has_conflict'):
            pts = '\n'.join([f"- {x}" for x in conflict.get('points', [])[:5]])
            conflict_card = f"⚠️ 存在冲突: {conflict.get('summary','')}\n{pts}"
        m.step('conflict', True, {'has_conflict': conflict.get('has_conflict', False), 'summary': conflict.get('summary','')})
        m.flag('has_conflict', bool(conflict.get('has_conflict', False)))
        print(f"✅ STEP 7 完成: has_conflict={bool(conflict.get('has_conflict', False))}")

        print("STEP 8/8 生成回答...")
        priority_ctx = build_priority_context(client, meta.get('priority_uris', []))
        ans = answer(query, local_txt, external_txt, priority_ctx=priority_ctx,
                     conflict_card=conflict_card, warnings=cv_warnings)

        # 回答透明度：附加来源和置信度信息
        source_info = _build_source_footer(meta, coverage, boost_needed, cv_warnings)
        ans = ans.rstrip() + "\n\n" + source_info
        m.step('answer', True, {'answer_len': len(ans), 'priority_uris': meta.get('priority_uris', [])})
        m.score('priority_uris_count', len(meta.get('priority_uris', [])))
        m.flag('ingested', ingested)
        m.score('answer_len', len(ans))
        report = m.finalize()

        case_path = None
        if os.getenv('CURATOR_CAPTURE_CASE', '1') in ('1','true','True'):
            case_path = capture_case(query, scope, report, ans, out_dir=os.getenv('CURATOR_CASE_DIR','cases'))

        print("\n===== FINAL ANSWER =====\n")
        print(ans)
        print("\n===== EVAL METRICS =====\n")
        print(json.dumps({
            'duration_sec': report['duration_sec'],
            'flags': report['flags'],
            'scores': report['scores'],
            'case_path': case_path
        }, ensure_ascii=False, indent=2))
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]).strip() or "grok2api 自动注册需要哪些前置配置和常见失败原因？"
    run(q)
