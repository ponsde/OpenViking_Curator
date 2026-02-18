#!/usr/bin/env python3
"""
search_providers.py — Pluggable external search provider abstraction.

Supported providers:
  - grok (default): Uses Grok via grok2api / OAI-compatible endpoint
  - oai:  Uses any OAI-compatible chat endpoint (e.g. ChatGPT with browsing)

Adding a new provider:
  1. Create a function: def my_provider(query, scope, **kwargs) -> str
  2. Register it: PROVIDERS["my_provider"] = my_provider
  3. Set env: CURATOR_SEARCH_PROVIDER=my_provider
"""

import datetime, os

def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


def _build_search_prompt(query: str, scope: dict) -> tuple[str, str]:
    """Build system + user prompt for search. Shared across providers."""
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
    return system, user


def _chat(base, key, model, messages, timeout=90):
    """Minimal OAI chat call (same as curator_v0.chat)."""
    import httpx
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": 0.3},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ── Provider: Grok ──
def grok_search(query: str, scope: dict, **kwargs) -> str:
    base = kwargs.get("base") or env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
    key = kwargs.get("key") or env("CURATOR_GROK_KEY")
    model = kwargs.get("model") or env("CURATOR_GROK_MODEL", "grok-4-fast")
    system, user = _build_search_prompt(query, scope)
    return _chat(base, key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout=90)


# ── Provider: OAI-compatible (any model with internet access) ──
def oai_search(query: str, scope: dict, **kwargs) -> str:
    base = kwargs.get("base") or env("CURATOR_OAI_BASE")
    key = kwargs.get("key") or env("CURATOR_OAI_KEY")
    model = kwargs.get("model") or env("CURATOR_SEARCH_OAI_MODEL", "gpt-4o")
    system, user = _build_search_prompt(query, scope)
    return _chat(base, key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout=90)


# ── Provider registry ──
PROVIDERS = {
    "grok": grok_search,
    "oai": oai_search,
}


def get_provider(name: str = None):
    """Get search provider function by name. Default from env or 'grok'."""
    name = name or env("CURATOR_SEARCH_PROVIDER", "grok")
    if name not in PROVIDERS:
        raise ValueError(f"Unknown search provider: {name}. Available: {list(PROVIDERS.keys())}")
    return PROVIDERS[name]


def search(query: str, scope: dict, provider: str = None, **kwargs) -> str:
    """Unified search entry point."""
    fn = get_provider(provider)
    return fn(query, scope, **kwargs)
