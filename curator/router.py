"""Router: 轻量路由 — 只判断领域和是否需要时效性信息。

不做完整 scope 分析（OV search 自带 VLM 意图分析）。
只给 external_search 提供最小必要信号。
"""

import json
import os
import re
from pathlib import Path

from .config import log

# ── Built-in defaults (used when config file is missing/invalid) ──────────────
_DEFAULT_DOMAIN_MAP = {
    "technology": [
        "docker",
        "nginx",
        "linux",
        "k8s",
        "kubernetes",
        "systemd",
        "git",
        "python",
        "asyncio",
        "rust",
        "golang",
        "javascript",
        "typescript",
        "api",
        "mcp",
        "rag",
        "llm",
        "openai",
        "claude",
        "grok",
        "embedding",
        "vector",
        "openviking",
        "grok2api",
        "wordpress",
        "cloudflare",
        "向量",
        "容器",
        "部署",
        "配置",
        "排查",
        "服务器",
        "数据库",
    ],
    "devops": [
        "vps",
        "ssh",
        "firewall",
        "防火墙",
        "安全加固",
        "监控",
        "systemctl",
        "journalctl",
        "iptables",
        "ufw",
    ],
}

_DEFAULT_TIME_KEYWORDS = ["最新", "更新", "release", "changelog", "2026", "2025", "latest"]


def _normalize_domain_map(domain_map: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for domain, terms in domain_map.items():
        if not isinstance(domain, str) or not isinstance(terms, list):
            continue
        cleaned = []
        for t in terms:
            if isinstance(t, str):
                v = t.strip().lower()
                if v:
                    cleaned.append(v)
        if cleaned:
            out[domain.strip()] = cleaned
    return out


def _normalize_time_keywords(keywords: list) -> list[str]:
    out: list[str] = []
    for k in keywords:
        if isinstance(k, str):
            v = k.strip().lower()
            if v:
                out.append(v)
    return out


def _load_router_config() -> tuple[dict[str, list[str]], list[str]]:
    """Load router config from JSON; fallback to built-in defaults.

    Search order:
      1) CURATOR_ROUTER_CONFIG (if set)
      2) ./router_config.json (project root / current working dir)
      3) curator/router_config.json (package default)
    """
    candidates = []
    env_path = os.getenv("CURATOR_ROUTER_CONFIG", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "router_config.json")
    candidates.append(Path(__file__).with_name("router_config.json"))

    for p in candidates:
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            domain_map = raw.get("domain_map", {}) if isinstance(raw, dict) else {}
            time_keywords = raw.get("time_keywords", []) if isinstance(raw, dict) else []

            norm_domain_map = _normalize_domain_map(domain_map)
            norm_time_keywords = _normalize_time_keywords(time_keywords)

            if norm_domain_map and norm_time_keywords:
                log.debug("router config loaded from %s", p)
                return norm_domain_map, norm_time_keywords
            log.warning("router config invalid/incomplete at %s; using fallback", p)
        except Exception as e:
            log.warning("failed to load router config from %s: %s; using fallback", p, e)

    # fallback
    return _normalize_domain_map(_DEFAULT_DOMAIN_MAP), _normalize_time_keywords(_DEFAULT_TIME_KEYWORDS)


_DOMAIN_MAP, _TIME_KEYWORDS = _load_router_config()


def route_scope(query: str) -> dict:
    """轻量路由：返回 domain + need_fresh + keywords。

    不做 LLM 调用（OV search 已经做了意图分析）。
    """
    ql = query.lower()

    # ── 领域判断（简单规则） ──
    domain = "general"
    for d, terms in _DOMAIN_MAP.items():
        if any(t in ql for t in terms):
            domain = d
            break

    # ── 时效性判断 ──
    need_fresh = any(k in ql for k in _TIME_KEYWORDS)

    # ── 关键词提取（简单分词，给 external_search 用） ──
    en_tokens = re.findall(r"[a-zA-Z0-9_\-/.]{3,}", query)
    cn_tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", query)
    keywords = list(dict.fromkeys(en_tokens + cn_tokens))[:6]

    return {
        "domain": domain,
        "keywords": keywords,
        "need_fresh": need_fresh,
    }
