#!/usr/bin/env python3
"""
search_providers.py — Pluggable external search provider abstraction.

Supported providers:
  - grok (default): Uses Grok via grok2api / OAI-compatible endpoint
  - oai:  Uses any OAI-compatible chat endpoint (e.g. ChatGPT with browsing)
  - duckduckgo: Uses duckduckgo-search PyPI package (no API key required)
  - tavily: Uses tavily-python PyPI package (requires CURATOR_TAVILY_KEY)

Provider chain (fallback):
  Set CURATOR_SEARCH_PROVIDERS=grok,duckduckgo,tavily (comma-separated).
  Providers are tried in order; first non-empty result wins.
  Default: grok

Adding a new provider:
  1. Create a function: def _search_myprovider(query, scope) -> str
  2. Register it: _PROVIDERS["myprovider"] = _search_myprovider
  3. Add it to CURATOR_SEARCH_PROVIDERS env var
"""

import datetime
import logging
import os

log = logging.getLogger("curator")


def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


def _build_search_prompt(query: str, scope: dict) -> tuple[str, str]:
    """Build system + user prompt for search. Shared across LLM-backed providers."""
    today = datetime.date.today().isoformat()
    system = (
        "你是实时搜索助手。重视可验证来源和信息时效性。"
        f"当前日期: {today}。"
        "对于技术类问题，优先引用官方文档和近期更新。"
        "如果搜到的信息可能已过时（如超过1年的项目、已变更的API流程），"
        "必须明确标注并提示用户验证。"
        "对于GitHub项目，务必区分：项目存在 ≠ 项目能用。"
    )
    user = (
        f"问题: {query}\n"
        f"关键词: {scope.get('keywords', [])}\n"
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
    return system, user


def _chat(base, key, model, messages, timeout=90):
    """OAI chat call. Uses requests (same as curator) for consistency."""
    import requests
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.3, "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError as e:
        ctype = r.headers.get("content-type", "")
        preview = (r.text or "")[:240].replace("\n", " ")
        raise RuntimeError(
            f"Non-JSON response from search provider (content-type={ctype}): {preview}"
        ) from e

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        err = payload.get("error") if isinstance(payload, dict) else payload
        raise RuntimeError(f"Invalid chat response payload: {err}")

    return choices[0]["message"]["content"]


# ── Provider: Grok ──
def _search_grok(query: str, scope: dict) -> str:
    """Search via Grok (OAI-compatible endpoint)."""
    base = env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
    key = env("CURATOR_GROK_KEY")
    model = env("CURATOR_GROK_MODEL", "grok-4-fast")
    system, user = _build_search_prompt(query, scope)
    return _chat(base, key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout=90)


# Keep legacy name for backward compatibility
def grok_search(query: str, scope: dict, **kwargs) -> str:
    """Legacy entry point — delegates to _search_grok."""
    return _search_grok(query, scope)


# ── Provider: OAI-compatible ──
def _search_oai(query: str, scope: dict) -> str:
    """Search via any OAI-compatible chat endpoint with internet access."""
    base = env("CURATOR_OAI_BASE")
    key = env("CURATOR_OAI_KEY")
    model = env("CURATOR_SEARCH_OAI_MODEL", "gpt-4o")
    system, user = _build_search_prompt(query, scope)
    return _chat(base, key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout=90)


def oai_search(query: str, scope: dict, **kwargs) -> str:
    """Legacy entry point — delegates to _search_oai."""
    return _search_oai(query, scope)


# ── Provider: DuckDuckGo ──
def _search_duckduckgo(query: str, scope: dict) -> str:
    """Search via DuckDuckGo (requires duckduckgo-search package)."""
    try:
        from duckduckgo_search import DDGS
    except ImportError as e:
        raise ImportError("duckduckgo-search not installed: pip install duckduckgo-search") from e

    results = DDGS().text(query, max_results=5)
    if not results:
        return ""

    parts = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        parts.append(f"**{title}**\n{href}\n{body}")

    return "\n\n".join(parts)


# ── Provider: Tavily ──
def _search_tavily(query: str, scope: dict) -> str:
    """Search via Tavily (requires tavily-python package + CURATOR_TAVILY_KEY)."""
    try:
        from tavily import TavilyClient
    except ImportError as e:
        raise ImportError("tavily-python not installed: pip install tavily-python") from e

    key = env("CURATOR_TAVILY_KEY")
    if not key:
        raise RuntimeError("CURATOR_TAVILY_KEY not configured; skipping Tavily")

    client = TavilyClient(api_key=key)
    response = client.search(query, max_results=5)

    results = response.get("results", []) if isinstance(response, dict) else []
    if not results:
        return ""

    parts = []
    for r in results:
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        parts.append(f"**{title}**\n{url}\n{content}")

    return "\n\n".join(parts)


# ── Provider registry ──
# Maps provider name → private function name (str, not direct reference).
# This indirection is intentional: search() resolves functions via getattr()
# at call time so that unittest.mock.patch.object() patches are picked up.
_PROVIDERS = {
    "grok": "_search_grok",
    "oai": "_search_oai",
    "duckduckgo": "_search_duckduckgo",
    "tavily": "_search_tavily",
}

# Legacy alias (kept for any code importing PROVIDERS directly)
PROVIDERS = {
    "grok": grok_search,
    "oai": oai_search,
}


def _call_provider(pname: str, query: str, scope: dict) -> str:
    """
    Resolve and call the provider function by name.

    Uses getattr on the current module so that unittest.mock.patch.object()
    patches applied to the module's function attributes are honoured.
    """
    import sys
    mod = sys.modules[__name__]
    fn_attr = _PROVIDERS.get(pname)
    if fn_attr is None:
        raise ValueError(f"Unknown provider {pname!r}")
    fn = getattr(mod, fn_attr)
    return fn(query, scope)


def _get_provider_chain() -> list[str]:
    """Return ordered list of providers to try, from CURATOR_SEARCH_PROVIDERS env var."""
    raw = env("CURATOR_SEARCH_PROVIDERS", env("CURATOR_SEARCH_PROVIDER", "grok"))
    chain = [p.strip() for p in raw.split(",") if p.strip()]
    # Filter out unknown providers with a warning
    known = []
    for p in chain:
        if p in _PROVIDERS:
            known.append(p)
        else:
            log.warning("Unknown search provider %r in CURATOR_SEARCH_PROVIDERS, skipping", p)
    return known or ["grok"]


def get_provider(name: str = None):
    """Get search provider function by name. Default from env or 'grok'."""
    import sys
    name = name or env("CURATOR_SEARCH_PROVIDER", "grok")
    if name not in _PROVIDERS:
        raise ValueError(
            f"Unknown search provider: {name}. Available: {list(_PROVIDERS.keys())}"
        )
    mod = sys.modules[__name__]
    return getattr(mod, _PROVIDERS[name])


def search(query: str, scope: dict, provider: str = None, **kwargs) -> str:
    """
    Unified search entry point with fallback chain.

    If `provider` is specified, only that provider is used (no fallback).
    Otherwise, iterates through CURATOR_SEARCH_PROVIDERS in order, returning
    the first non-empty result. Returns "" if all providers fail.
    """
    if provider:
        # Single-provider mode (backward compat / explicit override)
        return _call_provider(provider, query, scope)

    for pname in _get_provider_chain():
        try:
            result = _call_provider(pname, query, scope)
            if result.strip():
                return result
        except ImportError as e:
            log.warning("search provider %r unavailable (missing package): %s", pname, e)
        except Exception as e:
            log.warning("search provider %r failed: %s, trying next", pname, e)

    return ""  # All providers failed — pipeline will skip external search
