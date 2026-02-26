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

Concurrent mode (CURATOR_SEARCH_CONCURRENT=1):
  All providers are fired simultaneously; the fastest non-empty result wins.
  Set CURATOR_SEARCH_TIMEOUT to cap total wait time (default 60s).
  Set CURATOR_SEARCH_PROVIDER_TIMEOUT to cap each provider call and keep it
  below global timeout.

Adding a new provider:
  1. Create a function: def _search_myprovider(query, scope) -> str
  2. Register it: _PROVIDERS["myprovider"] = _search_myprovider
  3. Add it to CURATOR_SEARCH_PROVIDERS env var
"""

import asyncio
import datetime
import re
from dataclasses import dataclass
from typing import Any

from .config import (
    ALLOWED_DOMAINS,
    BLOCKED_DOMAINS,
    GROK_BASE,
    GROK_KEY,
    GROK_MODEL,
    OAI_BASE,
    OAI_KEY,
    SEARCH_PROVIDER_TIMEOUT,
    SEARCH_TIMEOUT,
    TAVILY_KEY,
    chat,
    env,
    log,
)

# Time-sensitive keywords (Chinese + English) — triggers date context injection
_TIME_KEYWORDS = re.compile(
    r"最新|最近|现在|今年|今天|当前|目前|近期|刚刚|更新|本周|上月|昨天"
    r"|latest|recent|current|now|today|new|updated|yesterday|last\s+week|20\d{2}",
    re.IGNORECASE,
)


@dataclass
class SearchResult:
    title: str
    url: str
    date: str = ""
    snippet: str = ""


def format_results(results: list[SearchResult]) -> str:
    """Format structured search results to markdown text (backward compatible)."""
    if not results:
        return ""
    parts = []
    for r in results:
        parts.append(f"**{r.title}**\n{r.url}\n{r.snippet}")
    return "\n\n".join(parts)


def _build_search_prompt(query: str, scope: dict) -> tuple[str, str]:
    """Build system + user prompt for search. Shared across LLM-backed providers."""
    from .domain_filter import build_domain_prompt_hint

    today = datetime.date.today().isoformat()
    domain_hint = build_domain_prompt_hint(ALLOWED_DOMAINS, BLOCKED_DOMAINS)

    # Inject explicit time context for time-sensitive queries
    time_ctx = ""
    if _TIME_KEYWORDS.search(query):
        now = datetime.datetime.now(datetime.timezone.utc)
        time_ctx = f"当前精确时间: {now.strftime('%Y-%m-%d %H:%M UTC')}。请确保搜索结果反映最新状态。"

    system = (
        "你是实时搜索助手。重视可验证来源和信息时效性。"
        f"当前日期: {today}。" + (f"{time_ctx}" if time_ctx else "") + "对于技术类问题，优先引用官方文档和近期更新。"
        "如果搜到的信息可能已过时（如超过1年的项目、已变更的API流程），"
        "必须明确标注并提示用户验证。"
        "对于GitHub项目，务必区分：项目存在 ≠ 项目能用。" + (f" {domain_hint}" if domain_hint else "")
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


# ── Provider: Grok ──
def _search_grok(query: str, scope: dict) -> str:
    """Search via Grok (OAI-compatible endpoint). Uses config.chat with retry."""
    system, user = _build_search_prompt(query, scope)
    return chat(
        GROK_BASE,
        GROK_KEY,
        GROK_MODEL,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        timeout=SEARCH_PROVIDER_TIMEOUT,
        temperature=0.3,
    )


# Keep legacy name for backward compatibility
def grok_search(query: str, scope: dict, **kwargs) -> str:
    """Legacy entry point — delegates to _search_grok."""
    return _search_grok(query, scope)


# ── Provider: OAI-compatible ──
def _search_oai(query: str, scope: dict) -> str:
    """Search via any OAI-compatible chat endpoint with internet access."""
    model = env("CURATOR_SEARCH_OAI_MODEL", "gpt-4o")
    system, user = _build_search_prompt(query, scope)
    return chat(
        OAI_BASE,
        OAI_KEY,
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        timeout=SEARCH_PROVIDER_TIMEOUT,
        temperature=0.3,
    )


def oai_search(query: str, scope: dict, **kwargs) -> str:
    """Legacy entry point — delegates to _search_oai."""
    return _search_oai(query, scope)


# ── Provider: DuckDuckGo ──
def _search_duckduckgo(query: str, scope: dict) -> list[SearchResult]:
    """Search via DuckDuckGo and return structured results."""
    try:
        from duckduckgo_search import DDGS
    except ImportError as e:
        raise ImportError("duckduckgo-search not installed: pip install duckduckgo-search") from e

    from .domain_filter import filter_results_by_domain

    results = DDGS().text(query, max_results=5)
    if not results:
        return []

    results = filter_results_by_domain(results, "href", ALLOWED_DOMAINS, BLOCKED_DOMAINS)
    if not results:
        return []

    out: list[SearchResult] = []
    for r in results:
        out.append(
            SearchResult(
                title=str(r.get("title", "")),
                url=str(r.get("href", "")),
                snippet=str(r.get("body", "")),
            )
        )
    return out


# ── Provider: Tavily ──
def _search_tavily(query: str, scope: dict) -> list[SearchResult]:
    """Search via Tavily and return structured results."""
    try:
        from tavily import TavilyClient
    except ImportError as e:
        raise ImportError("tavily-python not installed: pip install tavily-python") from e

    if not TAVILY_KEY:
        raise RuntimeError("CURATOR_TAVILY_KEY not configured; skipping Tavily")

    from .domain_filter import filter_results_by_domain

    client = TavilyClient(api_key=TAVILY_KEY)
    response = client.search(query, max_results=5)

    results = response.get("results", []) if isinstance(response, dict) else []
    if not results:
        return []

    results = filter_results_by_domain(results, "url", ALLOWED_DOMAINS, BLOCKED_DOMAINS)
    if not results:
        return []

    out: list[SearchResult] = []
    for r in results:
        out.append(
            SearchResult(
                title=str(r.get("title", "")),
                url=str(r.get("url", "")),
                snippet=str(r.get("content", "")),
            )
        )
    return out


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


def _call_provider(pname: str, query: str, scope: dict) -> str | list[SearchResult]:
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


def _provider_output_to_text(result: Any) -> str:
    """Convert provider output to markdown text (supports structured results)."""
    if isinstance(result, list):
        structured: list[SearchResult] = []
        for item in result:
            if isinstance(item, SearchResult):
                structured.append(item)
        return format_results(structured)
    if isinstance(result, str):
        return result
    return str(result or "")


def _get_provider_chain() -> list[str]:
    """Return ordered list of providers to try, from CURATOR_SEARCH_PROVIDERS env var."""
    raw = env("CURATOR_SEARCH_PROVIDERS", env("CURATOR_SEARCH_PROVIDER", "grok"))
    chain = [p.strip() for p in raw.split(",") if p.strip()]
    known = []
    for p in chain:
        if p in _PROVIDERS:
            known.append(p)
        else:
            log.warning("Unknown search provider %r in CURATOR_SEARCH_PROVIDERS, skipping", p)
    return known or ["grok"]


def get_provider(name: str | None = None):
    """Get search provider function by name. Default from env or 'grok'."""
    import sys

    name = name or env("CURATOR_SEARCH_PROVIDER", "grok")
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown search provider: {name}. Available: {list(_PROVIDERS.keys())}")
    mod = sys.modules[__name__]
    return getattr(mod, _PROVIDERS[name])


def search(query: str, scope: dict, provider: str | None = None, **kwargs) -> str:
    """
    Unified search entry point with fallback chain.

    If `provider` is specified, only that provider is used (no fallback).
    Otherwise, iterates through CURATOR_SEARCH_PROVIDERS in order, returning
    the first non-empty result. Returns "" if all providers fail.
    """
    if provider:
        return _provider_output_to_text(_call_provider(provider, query, scope))

    from .circuit_breaker import CircuitOpenError, get_breaker

    for pname in _get_provider_chain():
        breaker = get_breaker(f"search:{pname}")
        if not breaker.allow_request():
            log.warning("search provider %r circuit open, skipping", pname)
            continue
        try:
            result = _provider_output_to_text(_call_provider(pname, query, scope))
            if result.strip():
                breaker.record_success()
                return result
        except CircuitOpenError:
            log.warning("search provider %r circuit open (from chat), skipping", pname)
            continue
        except ImportError as e:
            log.warning("search provider %r unavailable (missing package): %s", pname, e)
        except Exception as e:
            breaker.record_failure()
            log.warning("search provider %r failed: %s, trying next", pname, e)

    return ""


# ── Concurrent search (async) ──


async def _async_search_provider(name: str, query: str, scope: dict) -> tuple[str, str]:
    """Single-provider async wrapper. Returns (provider_name, result_text)."""
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, _call_provider, name, query, scope)
    result = _provider_output_to_text(raw)
    return name, result


async def _gather_search(query: str, scope: dict, providers: list, timeout: float) -> str:
    """Fire all providers concurrently; return the first non-empty result.

    Uses asyncio.as_completed so we take the fastest winner and cancel the rest.
    On timeout, returns the best result seen so far (or "").
    """
    if not providers:
        return ""

    tasks = [asyncio.ensure_future(_async_search_provider(p, query, scope)) for p in providers]

    best = ""
    deadline = asyncio.get_event_loop().time() + timeout

    try:
        for coro in asyncio.as_completed(tasks, timeout=timeout):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                pname, result = await asyncio.wait_for(coro, timeout=remaining)
                if result and result.strip():
                    log.debug("concurrent search: winner=%s", pname)
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return result.strip()
                if not best and result:
                    best = result
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                pass
            except ImportError as e:
                log.warning("concurrent search provider unavailable: %s", e)
            except Exception as e:
                log.warning("concurrent search provider failed: %s", e)
    except asyncio.TimeoutError:
        log.warning("concurrent search: global timeout reached (%.1fs)", timeout)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return best


def search_concurrent(
    query: str,
    scope: dict,
    providers: list | None = None,
    timeout: float | None = None,
) -> str:
    """Synchronous entry point for concurrent multi-provider search.

    Fires all providers simultaneously and returns the fastest non-empty result.
    Falls back to "" if all providers fail or timeout expires.

    If an event loop is already running (e.g. called from an async pipeline),
    the coroutine is scheduled via run_coroutine_threadsafe in a new thread loop.
    Otherwise asyncio.run() is used.
    """
    if providers is None:
        providers = _get_provider_chain()
    if timeout is None:
        timeout = SEARCH_TIMEOUT

    coro = _gather_search(query, scope, providers, timeout)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import threading

        result_holder = {}

        def _run_in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result_holder["result"] = new_loop.run_until_complete(_gather_search(query, scope, providers, timeout))
            finally:
                new_loop.close()

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()
        t.join(timeout + 1)
        return result_holder.get("result", "")
    else:
        return asyncio.run(coro)
