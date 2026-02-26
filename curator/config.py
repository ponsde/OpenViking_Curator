"""Configuration: env vars, thresholds, logging, HTTP client."""

import logging
import os
import time
from pathlib import Path

import requests


def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


# ── Logging ──
log = logging.getLogger("curator")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG if os.getenv("CURATOR_DEBUG") else logging.INFO)


# ── Paths ──
OPENVIKING_CONFIG_FILE = env("OPENVIKING_CONFIG_FILE", str(Path.home() / ".openviking" / "ov.conf"))
DATA_PATH = env("CURATOR_DATA_PATH", str(Path.cwd() / "data"))
CURATED_DIR = env("CURATOR_CURATED_DIR", str(Path.cwd() / "curated"))

# ── API endpoints ──
OAI_BASE = env("CURATOR_OAI_BASE")
OAI_KEY = env("CURATOR_OAI_KEY")
from ._version import __version__ as _pkg_version

CURATOR_VERSION = env("CURATOR_VERSION", _pkg_version)

ROUTER_MODELS = [
    m.strip()
    for m in env(
        "CURATOR_ROUTER_MODELS",
        "gpt-5.3-codex,【Claude Code】Claude-Sonnet 4-6",
    ).split(",")
    if m.strip()
]
JUDGE_MODEL = env("CURATOR_JUDGE_MODEL", "gpt-5.3-codex")
JUDGE_MODELS = [
    m.strip()
    for m in env("CURATOR_JUDGE_MODELS", "gpt-5.3-codex,【Claude Code】Claude-Sonnet 4-6").split(",")
    if m.strip()
]
GROK_BASE = env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
GROK_KEY = env("CURATOR_GROK_KEY")
GROK_MODEL = env("CURATOR_GROK_MODEL", "grok-4-fast")

# Search providers: comma-separated, tried in order (fallback chain)
SEARCH_PROVIDERS = env("CURATOR_SEARCH_PROVIDERS", "grok")  # e.g. "grok,duckduckgo,tavily"
TAVILY_KEY = env("CURATOR_TAVILY_KEY", "")

# Domain filtering for external search results.
# ALLOWED_DOMAINS: if set, only results from these domains are kept.
# BLOCKED_DOMAINS: results from these domains are always removed.
# Both accept comma-separated lists; blocked takes precedence over allowed.
ALLOWED_DOMAINS = [d.strip().lower() for d in env("CURATOR_ALLOWED_DOMAINS", "").split(",") if d.strip()]
BLOCKED_DOMAINS = [d.strip().lower() for d in env("CURATOR_BLOCKED_DOMAINS", "").split(",") if d.strip()]

# Concurrent search mode: fire all providers in parallel, take fastest non-empty result
SEARCH_CONCURRENT = env("CURATOR_SEARCH_CONCURRENT", "0") == "1"
SEARCH_TIMEOUT = float(env("CURATOR_SEARCH_TIMEOUT", "60"))
# Per-provider timeout should be below global timeout so concurrent coordinator can
# still cancel/return before straggler calls fully block the run.
SEARCH_PROVIDER_TIMEOUT = float(env("CURATOR_SEARCH_PROVIDER_TIMEOUT", str(max(1.0, SEARCH_TIMEOUT - 5.0))))
if SEARCH_PROVIDER_TIMEOUT >= SEARCH_TIMEOUT:
    SEARCH_PROVIDER_TIMEOUT = max(1.0, SEARCH_TIMEOUT * 0.8)

# ── Async ingest ──
ASYNC_INGEST = env("CURATOR_ASYNC_INGEST", "0") == "1"

# ── Tunable thresholds ──
THRESHOLD_CURATED_OVERLAP = float(env("CURATOR_THRESHOLD_CURATED_OVERLAP", "0.25"))
THRESHOLD_CURATED_MIN_HITS = int(env("CURATOR_THRESHOLD_CURATED_MIN_HITS", "3"))

# Retrieval: L0/L1/L2 on-demand loading thresholds
THRESHOLD_L0_SUFFICIENT = float(env("CURATOR_THRESHOLD_L0_SUFFICIENT", "0.62"))
THRESHOLD_L1_SUFFICIENT = float(env("CURATOR_THRESHOLD_L1_SUFFICIENT", "0.5"))

# Coverage assessment thresholds
THRESHOLD_COV_SUFFICIENT = float(env("CURATOR_THRESHOLD_COV_SUFFICIENT", "0.55"))
THRESHOLD_COV_MARGINAL = float(env("CURATOR_THRESHOLD_COV_MARGINAL", "0.45"))
THRESHOLD_COV_LOW = float(env("CURATOR_THRESHOLD_COV_LOW", "0.35"))

# feedback reranking
# Max score delta applied by feedback signals (keeps OV score dominant).
FEEDBACK_WEIGHT = float(env("CURATOR_FEEDBACK_WEIGHT", "0.10"))

# L2 full-read depth: max number of items to load at L2 per pipeline run
MAX_L2_DEPTH = int(env("CURATOR_MAX_L2_DEPTH", "2"))

# L0/L1 auto-summarization on ingest (opt-in, requires OAI_BASE)
# When enabled, ingest_markdown_v2 calls LLM once to generate:
#   L0 abstract (~80 tokens): stored in meta + header comment
#   L1 overview (key-point list): prepended to markdown as '## 摘要' section
AUTO_SUMMARIZE = env("CURATOR_AUTO_SUMMARIZE", "0") == "1"
SUMMARIZE_MODELS = [
    m.strip()
    for m in env("CURATOR_SUMMARIZE_MODELS", env("CURATOR_ROUTER_MODELS", "gpt-5.3-codex")).split(",")
    if m.strip()
]

# ── Circuit breaker ──
CB_ENABLED = env("CURATOR_CB_ENABLED", "1") == "1"
CB_THRESHOLD = int(env("CURATOR_CB_THRESHOLD", "3"))
CB_RECOVERY_SEC = float(env("CURATOR_CB_RECOVERY_SEC", "30"))

# ── Search cache ──
CACHE_ENABLED = env("CURATOR_CACHE_ENABLED", "0") == "1"
CACHE_TTL = int(env("CURATOR_CACHE_TTL", "3600"))
CACHE_FRESH_TTL = int(env("CURATOR_CACHE_FRESH_TTL", "300"))
CACHE_MAX_ENTRIES = int(env("CURATOR_CACHE_MAX_ENTRIES", "200"))

# Chat retry (lightweight, dependency-free)
CHAT_RETRY_MAX = max(1, int(env("CURATOR_CHAT_RETRY_MAX", "3")))
CHAT_RETRY_BACKOFF_SEC = max(0.0, float(env("CURATOR_CHAT_RETRY_BACKOFF_SEC", "0.6")))

FAST_ROUTE = env("CURATOR_FAST_ROUTE", "1") == "1"


def validate_config() -> None:
    missing = []
    if not OAI_BASE:
        missing.append("CURATOR_OAI_BASE")
    if not OAI_KEY:
        missing.append("CURATOR_OAI_KEY")
    # Check first provider in chain requires a key
    first_provider = SEARCH_PROVIDERS.split(",")[0].strip()
    if first_provider in ("grok",) and not GROK_KEY:
        missing.append("CURATOR_GROK_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Hint: copy .env.example to .env and fill in your API keys."
        )


def _should_retry_chat_error(err: Exception) -> bool:
    """Retry only transient transport/server failures."""
    if isinstance(err, requests.HTTPError):
        resp = getattr(err, "response", None)
        if resp is None:
            return True
        code = getattr(resp, "status_code", 0) or 0
        return code == 429 or code >= 500

    if isinstance(err, (requests.Timeout, requests.ConnectionError)):
        return True

    if isinstance(err, requests.RequestException):
        return True

    # RuntimeError from non-JSON / invalid payload is usually deterministic.
    return False


def chat(base, key, model, messages, timeout=60, temperature=None):
    """OAI-compatible chat completion call with lightweight retries.

    Circuit breaker wraps the entire retry loop: one chat() invocation
    (which may include multiple retry attempts) counts as a single
    success or failure for the breaker.
    """
    from .circuit_breaker import CircuitOpenError, get_breaker

    breaker = get_breaker(f"chat:{model}")
    if not breaker.allow_request():
        raise CircuitOpenError(f"circuit open for chat:{model}")

    last_err = None
    retry_max = max(1, CHAT_RETRY_MAX)
    body = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        body["temperature"] = temperature

    for attempt in range(1, retry_max + 1):
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            r.raise_for_status()
            try:
                payload = r.json()
            except ValueError as e:
                ctype = r.headers.get("content-type", "")
                preview = (r.text or "")[:240].replace("\n", " ")
                raise RuntimeError(f"Non-JSON response from chat API (content-type={ctype}): {preview}") from e

            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not choices:
                err = payload.get("error") if isinstance(payload, dict) else payload
                raise RuntimeError(f"Invalid chat response payload: {err}")

            breaker.record_success()
            return choices[0]["message"]["content"]
        except Exception as e:
            last_err = e
            can_retry = attempt < retry_max and _should_retry_chat_error(e)
            if not can_retry:
                break
            sleep_s = CHAT_RETRY_BACKOFF_SEC * attempt
            log.warning("chat retry %d/%d model=%s error=%s", attempt, retry_max, model, e)
            time.sleep(sleep_s)

    breaker.record_failure()
    raise RuntimeError(f"chat failed after retries: {last_err}") from last_err
