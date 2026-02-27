"""Configuration: env vars, thresholds, logging, HTTP client.

All values are sourced from ``CuratorSettings`` (pydantic-settings) for type
safety and validation.  Module-level aliases preserve backward compatibility —
no consumer code needs to change.
"""

import os
import time

import requests

from ._version import __version__ as _pkg_version
from .logging_setup import configure_logging
from .settings import CuratorSettings


def env(name: str, default: str = "") -> str:
    """Read an env var (still used by dedup.py and a few others)."""
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else v


# ── Settings singleton ──
_settings = CuratorSettings()

# ── Logging (structlog bridge with JSON toggle) ──
log = configure_logging()


# ── Paths ──
OPENVIKING_CONFIG_FILE = _settings.openviking_config_file
DATA_PATH = _settings.data_path
CURATED_DIR = _settings.curated_dir

# ── API endpoints ──
OAI_BASE = _settings.oai_base
OAI_KEY = _settings.oai_key

CURATOR_VERSION = _settings.version or _pkg_version

ROUTER_MODELS = [m.strip() for m in _settings.router_models.split(",") if m.strip()]
JUDGE_MODELS = [m.strip() for m in _settings.judge_models.split(",") if m.strip()] or ROUTER_MODELS
JUDGE_MODEL = _settings.judge_model or (JUDGE_MODELS[0] if JUDGE_MODELS else "")
GROK_BASE = _settings.grok_base
GROK_KEY = _settings.grok_key
GROK_MODEL = _settings.grok_model

# Search providers: comma-separated, tried in order (fallback chain)
SEARCH_PROVIDERS = _settings.search_providers
TAVILY_KEY = _settings.tavily_key

# Domain filtering for external search results.
ALLOWED_DOMAINS = [d.strip().lower() for d in _settings.allowed_domains.split(",") if d.strip()]
BLOCKED_DOMAINS = [d.strip().lower() for d in _settings.blocked_domains.split(",") if d.strip()]
DOMAIN_FILTER_STRICT = _settings.domain_filter_strict in ("1", "true", "yes")

# Concurrent search mode
SEARCH_CONCURRENT = _settings.search_concurrent == "1"
SEARCH_TIMEOUT = _settings.search_timeout
SEARCH_PROVIDER_TIMEOUT = _settings.search_provider_timeout
SEARCH_MAX_INFLIGHT = _settings.search_max_inflight

# ── Async ingest ──
ASYNC_INGEST = _settings.async_ingest == "1"

# ── Tunable thresholds ──
THRESHOLD_CURATED_OVERLAP = _settings.threshold_curated_overlap
THRESHOLD_CURATED_MIN_HITS = _settings.threshold_curated_min_hits

# Retrieval: L0/L1/L2 on-demand loading thresholds
THRESHOLD_L0_SUFFICIENT = _settings.threshold_l0_sufficient
THRESHOLD_L1_SUFFICIENT = _settings.threshold_l1_sufficient

# Coverage assessment thresholds
THRESHOLD_COV_SUFFICIENT = _settings.threshold_cov_sufficient
THRESHOLD_COV_MARGINAL = _settings.threshold_cov_marginal
THRESHOLD_COV_LOW = _settings.threshold_cov_low

# feedback reranking
FEEDBACK_WEIGHT = _settings.feedback_weight
FEEDBACK_DECAY_ENABLED = _settings.feedback_decay_enabled in ("1", "true", "yes")
FEEDBACK_HALF_LIFE_DAYS = _settings.feedback_half_life_days
FEEDBACK_ADOPT_COEF = _settings.feedback_adopt_coef
FEEDBACK_DOWN_COEF = _settings.feedback_down_coef
FEEDBACK_EXPLORE_BONUS = _settings.feedback_explore_bonus
FEEDBACK_SMOOTH = _settings.feedback_smooth

# L2 full-read depth
MAX_L2_DEPTH = _settings.max_l2_depth

# Retrieval limit and feedback adopt threshold
RETRIEVE_LIMIT = _settings.retrieve_limit
ADOPT_MIN_SCORE = _settings.adopt_min_score

# L0/L1 auto-summarization on ingest
AUTO_SUMMARIZE = _settings.auto_summarize == "1"
SUMMARIZE_MODELS = [m.strip() for m in (_settings.summarize_models or _settings.router_models).split(",") if m.strip()]

# ── Circuit breaker ──
CB_ENABLED = _settings.cb_enabled == "1"
CB_THRESHOLD = _settings.cb_threshold
CB_RECOVERY_SEC = _settings.cb_recovery_sec

# ── Search cache ──
CACHE_ENABLED = _settings.cache_enabled == "1"
CACHE_TTL = _settings.cache_ttl
CACHE_FRESH_TTL = _settings.cache_fresh_ttl
CACHE_MAX_ENTRIES = _settings.cache_max_entries

# Chat retry
CHAT_RETRY_MAX = max(1, _settings.chat_retry_max)
CHAT_RETRY_BACKOFF_SEC = max(0.0, _settings.chat_retry_backoff_sec)

FAST_ROUTE = _settings.fast_route == "1"

# ── Log rotation ──
LOG_ROTATE_MB = _settings.log_rotate_mb
LOG_ROTATE_KEEP = _settings.log_rotate_keep

# ── Dedup ──
DEDUP_SIMILARITY = _settings.dedup_similarity
DEDUP_MAX_ITEMS = _settings.dedup_max_items
DEDUP_LOG = _settings.dedup_log  # empty string means auto-derive in dedup.py


def validate_config() -> None:
    missing = []
    if not OAI_BASE:
        missing.append("CURATOR_OAI_BASE")
    if not OAI_KEY:
        missing.append("CURATOR_OAI_KEY")
    if not ROUTER_MODELS:
        missing.append("CURATOR_ROUTER_MODELS")
    first_provider = SEARCH_PROVIDERS.split(",")[0].strip()
    if first_provider in ("grok",):
        if not GROK_BASE:
            missing.append("CURATOR_GROK_BASE")
        if not GROK_KEY:
            missing.append("CURATOR_GROK_KEY")
    if first_provider == "tavily" and not TAVILY_KEY:
        missing.append("CURATOR_TAVILY_KEY")
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

    # Permanent config errors (e.g. empty base URL) — do not retry
    if isinstance(err, (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL)):
        return False

    if isinstance(err, requests.RequestException):
        return True

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
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if content is None:
                raise RuntimeError(f"Missing content in chat response choices[0]: {choices[0]}")
            return content
        except Exception as e:
            last_err = e
            can_retry = attempt < retry_max and _should_retry_chat_error(e)
            if not can_retry:
                break
            sleep_s = CHAT_RETRY_BACKOFF_SEC * attempt
            log.warning("chat retry %d/%d model=%s error=%s", attempt, retry_max, model, e)
            time.sleep(sleep_s)

    # Only trip the circuit breaker for transient failures (network/server errors).
    # Permanent failures (bad auth, wrong model, malformed request) are caller bugs
    # and should not cause the breaker to reject subsequent valid requests.
    if last_err is not None and _should_retry_chat_error(last_err):
        breaker.record_failure()
    raise RuntimeError(f"chat failed after retries: {last_err}") from last_err
