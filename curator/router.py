"""Router: 轻量路由 — 只判断领域和是否需要时效性信息。

不做完整 scope 分析（OV search 自带 VLM 意图分析）。
只给 external_search 提供最小必要信号。
"""

import re
from .config import log, FAST_ROUTE


def route_scope(query: str) -> dict:
    """轻量路由：返回 domain + need_fresh + keywords。

    不做 LLM 调用（OV search 已经做了意图分析）。
    """
    ql = query.lower()

    # ── 领域判断（简单规则） ──
    _DOMAIN_MAP = {
        "technology": ["docker", "nginx", "linux", "k8s", "kubernetes", "systemd", "git",
                       "python", "asyncio", "rust", "golang", "javascript", "typescript",
                       "api", "mcp", "rag", "llm", "openai", "claude", "grok", "embedding",
                       "vector", "openviking", "grok2api", "wordpress", "cloudflare",
                       "向量", "容器", "部署", "配置", "排查", "服务器", "数据库"],
        "devops": ["vps", "ssh", "firewall", "防火墙", "安全加固", "监控",
                   "systemctl", "journalctl", "iptables", "ufw"],
    }
    domain = "general"
    for d, terms in _DOMAIN_MAP.items():
        if any(t in ql for t in terms):
            domain = d
            break

    # ── 时效性判断 ──
    need_fresh = any(k in ql for k in ["最新", "更新", "release", "changelog", "2026", "2025", "latest"])

    # ── 关键词提取（简单分词，给 external_search 用） ──
    en_tokens = re.findall(r"[a-zA-Z0-9_\-/.]{3,}", query)
    cn_tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", query)
    keywords = list(dict.fromkeys(en_tokens + cn_tokens))[:6]

    return {
        "domain": domain,
        "keywords": keywords,
        "need_fresh": need_fresh,
    }
