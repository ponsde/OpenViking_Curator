"""Configuration: env vars, thresholds, logging, HTTP client."""

import os
import logging
import requests
from pathlib import Path


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
GROK_BASE = env("CURATOR_GROK_BASE", "http://127.0.0.1:8000/v1")
GROK_KEY = env("CURATOR_GROK_KEY")
GROK_MODEL = env("CURATOR_GROK_MODEL", "grok-4-fast")

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

FAST_ROUTE = env("CURATOR_FAST_ROUTE", "1") == "1"

# ── Module-level constants ──
_GENERIC_TERMS = {
    "2.0", "3.0", "1.0", "0.1", "2025", "2026", "2024", "最新", "latest",
    "对比", "比较", "区别", "最佳", "实践", "方案", "选型", "推荐",
    "怎么", "如何", "什么", "为什么", "哪些", "入门", "指南",
    "compare", "best", "practice", "guide", "tutorial", "how",
    "vs", "versus", "performance", "benchmark",
}


def validate_config() -> None:
    missing = []
    if not OAI_BASE:
        missing.append("CURATOR_OAI_BASE")
    if not OAI_KEY:
        missing.append("CURATOR_OAI_KEY")
    search_provider = env("CURATOR_SEARCH_PROVIDER", "grok")
    if search_provider == "grok" and not GROK_KEY:
        missing.append("CURATOR_GROK_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Hint: copy .env.example to .env and fill in your API keys."
        )


def chat(base, key, model, messages, timeout=60):
    """OAI-compatible chat completion call."""
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False},
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

    return choices[0]["message"]["content"]
