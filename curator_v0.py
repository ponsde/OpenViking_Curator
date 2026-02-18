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


def route_scope(query: str):
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
    # 额外加一个纯 query（无关键词后缀）
    search_queries.append(query)

    for sq in search_queries:
        for method in (client.find, client.search):
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
                           or relevance >= 0.55)

    # 噪声惩罚：证据弱但关键词覆盖高
    if evidence_ratio < 0.15 and kw_cov > 0.5:
        kw_cov = kw_cov * 0.35

    coverage = max(kw_cov, relevance) if effective_domain_hit else min(max(kw_cov, relevance), 0.18)

    # curated 资源加权：搜到我们入库过的文档说明知识库里有相关内容
    def _is_our_doc(u):
        ul = u.lower()
        return any(tag in ul for tag in ("curated", "single_", "reingest_", "fix_", "re2_"))
    curated_uris = [u for u in uris if _is_our_doc(u)]
    if curated_uris:
        # 用 query + 关键词在正文中匹配（宽松：中英文都查）
        query_terms = set(kw) | set(re.findall(r"[\u4e00-\u9fff]{2,}", query))
        preview_text = " ".join(previews).lower()
        content_overlap = sum(1 for t in query_terms if t and t.lower() in preview_text)
        overlap_ratio = content_overlap / max(1, len(query_terms))
        if overlap_ratio >= 0.15 or content_overlap >= 2:
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

    # 覆盖率阈值（已知内部域名可更宽松，减少重复外搜）
    low_cov_threshold = 0.45
    if any(k in q for k in ["newapi", "openviking", "grok2api", "mcp"]):
        low_cov_threshold = 0.35

    if coverage < low_cov_threshold:
        return True, "low_coverage"
    if need_fresh and (low_fresh or low_quality):
        return True, "freshness_or_quality_boost"
    if need_fresh and weak_feedback and low_quality:
        return True, "need_fresh_no_positive_feedback"
    return False, "local_sufficient"


def external_search(query: str, scope: dict):
    prompt = (
        f"问题: {query}\n"
        f"关键词: {scope.get('keywords', [])}\n"
        f"排除: {scope.get('exclude', [])}\n"
        f"偏好来源: {scope.get('source_pref', [])}\n"
        "请搜索并返回5条高质量来源，格式：标题+URL+关键点。"
    )
    return chat(GROK_BASE, GROK_KEY, GROK_MODEL, [
        {"role": "system", "content": "你是实时搜索助手，重视可验证来源。"},
        {"role": "user", "content": prompt},
    ], timeout=90)


def judge_and_pack(query: str, external_text: str):
    sys = (
        "你是资料审核器。判断外部搜索结果是否值得入库。"
        "输出严格JSON: pass(bool), reason(string), tags(array), trust(0-10), summary(string), markdown(string)。"
        "markdown要求包含来源URL。只输出JSON。"
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


def ingest_markdown(client, title: str, markdown: str):
    p = Path(CURATED_DIR)
    p.mkdir(parents=True, exist_ok=True)
    fn = p / f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', title)[:40]}.md"
    fn.write_text(markdown, encoding="utf-8")
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


def answer(query: str, local_ctx: str, external_ctx: str, priority_ctx: str = "", conflict_card: str = ""):
    sys = "你是技术助手。基于给定上下文回答，最后给来源列表。若存在冲突卡片，先展示冲突再给建议。"
    user = f"问题:\n{query}\n\n冲突卡片:\n{conflict_card}\n\n优先来源上下文:\n{priority_ctx[:2500]}\n\n本地上下文:\n{local_ctx[:5000]}\n\n外部补充:\n{external_ctx[:3000]}"

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


def run(query: str):
    m = Metrics()
    validate_config()
    os.environ["OPENVIKING_CONFIG_FILE"] = OPENVIKING_CONFIG_FILE

    print("STEP 1/6 初始化...")
    client = ov.SyncOpenViking(path=DATA_PATH)
    client.initialize()
    m.step('init', True)
    print("✅ STEP 1 完成")

    try:
        print("STEP 2/6 范围路由...")
        scope = route_scope(query)
        m.step('route', True, {'domain': scope.get('domain'), 'confidence': scope.get('confidence')})
        m.score('router_confidence', scope.get('confidence', 0))
        print("✅ STEP 2 完成:", json.dumps(scope, ensure_ascii=False))

        print("STEP 3/6 本地检索(OpenViking)...")
        local_txt, coverage, meta = local_search(client, query, scope)
        m.step('local_search', True, {'coverage': coverage, 'kw_cov': meta.get('kw_cov'), 'domain_hit': meta.get('domain_hit')})
        m.score('coverage_before_external', round(coverage, 3))
        print(
            f"✅ STEP 3 完成: coverage={coverage:.2f}, kw_cov={meta['kw_cov']:.2f}, "
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
            print(f"STEP 4/6 触发外部搜索(Grok)... reason={boost_reason}")
            external_txt = external_search(query, scope)
            m.step('external_search', True, {'len': len(external_txt), 'reason': boost_reason})
            print("✅ STEP 4 完成: 外部结果长度", len(external_txt))

            print("STEP 5/6 审核并尝试入库...")
            j = judge_and_pack(query, external_txt)
            m.step('judge', True, {'pass': j.get('pass'), 'trust': j.get('trust')})
            print("审核结果:", json.dumps({k: j.get(k) for k in ["pass", "reason", "trust", "tags"]}, ensure_ascii=False))
            if j.get("pass") and j.get("markdown"):
                ing = ingest_markdown(client, "curated", j["markdown"])
                ingested = True
                m.step('ingest', True, {'uri': ing.get('root_uri', '')})
                print("✅ 已入库:", ing.get("root_uri", ""))
            else:
                m.step('ingest', False)
                print("⚠️ 未入库")
        else:
            m.flag('external_triggered', False)
            m.flag('external_reason', boost_reason)
            print("STEP 4/6 跳过外部搜索（本地覆盖与质量足够）")

        print("STEP 6/7 冲突检测...")
        conflict = detect_conflict(query, local_txt, external_txt)
        conflict_card = ""
        if conflict.get('has_conflict'):
            pts = '\n'.join([f"- {x}" for x in conflict.get('points', [])[:5]])
            conflict_card = f"⚠️ 存在冲突: {conflict.get('summary','')}\n{pts}"
        m.step('conflict', True, {'has_conflict': conflict.get('has_conflict', False), 'summary': conflict.get('summary','')})
        m.flag('has_conflict', bool(conflict.get('has_conflict', False)))
        print(f"✅ STEP 6 完成: has_conflict={bool(conflict.get('has_conflict', False))}")

        print("STEP 7/7 生成回答...")
        priority_ctx = build_priority_context(client, meta.get('priority_uris', []))
        ans = answer(query, local_txt, external_txt, priority_ctx=priority_ctx, conflict_card=conflict_card)
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
