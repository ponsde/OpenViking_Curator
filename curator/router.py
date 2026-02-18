"""Router: scope routing (rule-based + LLM fallback)."""

import json
import re

from .config import (
    env, log, chat,
    OAI_BASE, OAI_KEY, ROUTER_MODELS, FAST_ROUTE,
    _GENERIC_TERMS,
)


def _rule_based_scope(query: str) -> dict:
    """纯规则路由：0 API 调用，<1ms 完成"""
    ql = query.lower()

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

    en_tokens = re.findall(r"[a-zA-Z0-9_\-/.]{2,}", query)

    _CN_EXTRA_TERMS = {
        "所有权", "模型", "排查", "配置", "注册", "入门", "对比", "选型",
        "安全", "加固", "防火墙", "日志", "网络", "存储", "容器", "反向代理",
        "常见问题", "最佳实践", "工作原理", "使用场景", "设计理念", "快速上手",
        "自动更新", "兼容性", "参数差异", "注意事项", "网关对比", "状态管理",
        "上下文", "文件系统", "向量数据库", "陷阱", "高并发", "负载均衡",
        "微服务", "消息队列", "缓存", "数据库", "中间件", "监控", "告警",
    }
    cn_text = re.sub(r"[^\u4e00-\u9fff]", "", query)
    try:
        import jieba
        if not getattr(_rule_based_scope, '_jieba_init', False):
            for term in _CN_EXTRA_TERMS:
                jieba.add_word(term)
            _rule_based_scope._jieba_init = True
        cn_tokens = [w for w in jieba.cut(cn_text) if len(w) >= 2]
    except ImportError:
        cn_tokens = []
        remaining = cn_text
        while remaining:
            matched = False
            for length in (4, 3, 2):
                if len(remaining) >= length and remaining[:length] in _CN_EXTRA_TERMS:
                    cn_tokens.append(remaining[:length])
                    remaining = remaining[length:]
                    matched = True
                    break
            if not matched:
                remaining = remaining[1:]
        bigrams = re.findall(r"[\u4e00-\u9fff]{2}", query)
        for bg in bigrams:
            if bg in _CN_EXTRA_TERMS and bg not in cn_tokens:
                cn_tokens.append(bg)
    cn_tokens = list(dict.fromkeys(cn_tokens))

    _STOP = _GENERIC_TERMS | {
        "是什么", "怎么", "如何", "什么", "哪些", "常见", "有哪些",
        "怎么样", "可以", "应该", "到底", "一下", "这个", "那个",
        "the", "what", "how", "is", "are", "and", "for", "with", "to", "in", "of",
    }
    keywords = [t for t in (en_tokens + cn_tokens) if t.lower() not in _STOP and len(t) > 1]
    keywords = list(dict.fromkeys(keywords))[:8]

    need_fresh = any(k in ql for k in ["最新", "更新", "release", "changelog", "2026", "2025", "latest"])

    return {
        "domain": domain,
        "keywords": keywords,
        "exclude": [],
        "need_fresh": need_fresh,
        "source_pref": ["official_docs", "tech_blog", "github"],
        "confidence": 0.7,
    }


def route_scope(query: str):
    if FAST_ROUTE:
        return _rule_based_scope(query)

    sys_prompt = (
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
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": query},
            ], timeout=45)
            chosen = m
            break
        except Exception as e:
            last_err = e
            continue
    if out is None:
        raise RuntimeError(f"all router models failed: {last_err}")
    log.debug("router_model_used=%s", chosen)

    import re as _re
    m = _re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"domain": "general", "keywords": [query], "exclude": [],
                "need_fresh": True, "source_pref": ["official docs", "github"], "confidence": 0.5}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"domain": "general", "keywords": [query], "exclude": [],
                "need_fresh": True, "source_pref": ["official docs", "github"], "confidence": 0.5}
