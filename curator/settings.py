"""Typed, validated configuration using Pydantic Settings v2.

All 40+ env vars are declared here with types, defaults, and constraints.
``config.py`` imports a singleton ``_settings`` and re-exports module-level
aliases so that **no consumer code changes** are required.
"""

from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


def _csv_list(raw: str) -> List[str]:
    """Split comma-separated string into a stripped, non-empty list."""
    return [s.strip() for s in raw.split(",") if s.strip()]


class CuratorSettings(BaseSettings):
    """Curator configuration — every field maps to an env var."""

    model_config = {
        "env_prefix": "CURATOR_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # ── Paths ──
    openviking_config_file: str = Field(
        default=str(Path.home() / ".openviking" / "ov.conf"),
        validation_alias="OPENVIKING_CONFIG_FILE",
    )
    data_path: str = Field(default=str(Path.cwd() / "data"))
    curated_dir: str = Field(default=str(Path.cwd() / "curated"))

    # ── API endpoints ──
    oai_base: str = ""
    oai_key: str = ""

    router_models: str = "gpt-5.3-codex,【Claude Code】Claude-Sonnet 4-6"
    judge_model: str = "gpt-5.3-codex"
    judge_models: str = "gpt-5.3-codex,【Claude Code】Claude-Sonnet 4-6"

    grok_base: str = "http://127.0.0.1:8000/v1"
    grok_key: str = ""
    grok_model: str = "grok-4-fast"

    # ── Search ──
    search_providers: str = "grok"
    tavily_key: str = Field(default="", validation_alias="CURATOR_TAVILY_KEY")
    allowed_domains: str = ""
    blocked_domains: str = ""
    search_concurrent: str = "0"
    search_timeout: float = Field(default=60.0, ge=1.0)
    search_provider_timeout: float = Field(default=55.0, ge=1.0)

    # ── Async ingest ──
    async_ingest: str = "0"

    # ── Thresholds ──
    threshold_curated_overlap: float = Field(default=0.25, ge=0.0, le=1.0)
    threshold_curated_min_hits: int = Field(default=3, ge=0)
    threshold_l0_sufficient: float = Field(default=0.62, ge=0.0, le=1.0)
    threshold_l1_sufficient: float = Field(default=0.5, ge=0.0, le=1.0)
    threshold_cov_sufficient: float = Field(default=0.55, ge=0.0, le=1.0)
    threshold_cov_marginal: float = Field(default=0.45, ge=0.0, le=1.0)
    threshold_cov_low: float = Field(default=0.35, ge=0.0, le=1.0)

    # ── Feedback ──
    feedback_weight: float = Field(default=0.10, ge=0.0, le=1.0)

    # ── Retrieval ──
    max_l2_depth: int = Field(default=2, ge=0)

    # ── Auto-summarize ──
    auto_summarize: str = "0"
    summarize_models: str = ""

    # ── Circuit breaker ──
    cb_enabled: str = "1"
    cb_threshold: int = Field(default=3, ge=1)
    cb_recovery_sec: float = Field(default=30.0, ge=1.0)

    # ── Search cache ──
    cache_enabled: str = "0"
    cache_ttl: int = Field(default=3600, ge=0)
    cache_fresh_ttl: int = Field(default=300, ge=0)
    cache_max_entries: int = Field(default=200, ge=1)

    # ── Chat retry ──
    chat_retry_max: int = Field(default=3, ge=1)
    chat_retry_backoff_sec: float = Field(default=0.6, ge=0.0)

    # ── Misc ──
    fast_route: str = "1"
    version: str = ""
    debug: str = ""

    # ── Logging ──
    json_logging: str = "0"

    @field_validator("search_provider_timeout")
    @classmethod
    def _clamp_provider_timeout(cls, v: float, info) -> float:
        """Ensure provider timeout stays below global search timeout."""
        search_to = info.data.get("search_timeout", 60.0)
        if v >= search_to:
            return max(1.0, search_to * 0.8)
        return v


def get_settings(**overrides) -> CuratorSettings:
    """Create a CuratorSettings instance, optionally with overrides for testing."""
    return CuratorSettings(**overrides)
