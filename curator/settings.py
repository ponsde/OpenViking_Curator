"""Typed, validated configuration using Pydantic Settings v2.

All 40+ env vars are declared here with types, defaults, and constraints.
``config.py`` imports a singleton ``_settings`` and re-exports module-level
aliases so that **no consumer code changes** are required.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


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

    router_models: str = "gpt-4o-mini"
    judge_model: str = ""  # empty → falls back to first of JUDGE_MODELS
    judge_models: str = ""  # empty → falls back to ROUTER_MODELS

    grok_base: str = ""  # user must configure (any OAI-compatible endpoint)
    grok_key: str = ""
    grok_model: str = "grok-4-fast"

    # ── Search ──
    search_providers: str = "oai"
    tavily_key: str = Field(default="", validation_alias="CURATOR_TAVILY_KEY")
    allowed_domains: str = ""
    blocked_domains: str = ""
    domain_filter_strict: str = "0"  # "1" → drop no-URL results in allowlist mode
    search_concurrent: str = "0"
    search_timeout: float = Field(default=60.0, ge=1.0)
    search_provider_timeout: float = Field(default=55.0, ge=1.0)
    search_max_inflight: int = Field(default=0, ge=0)  # 0 = unlimited

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
    # Time-decay settings (prevent high-adopt content from dominating permanently)
    feedback_decay_enabled: str = "1"  # "1" to enable decay, "0" to keep legacy behaviour
    feedback_half_life_days: float = Field(default=14.0, ge=1.0)  # decay half-life in days
    feedback_adopt_coef: float = Field(default=1.5, ge=0.0)  # adopt weight multiplier
    feedback_down_coef: float = Field(default=1.2, ge=0.0)  # down weight multiplier
    feedback_explore_bonus: float = Field(default=0.05, ge=0.0)  # exploration boost for new content
    feedback_smooth: float = Field(default=1.0, ge=0.0)  # smoothing denominator term

    # ── Retrieval ──
    max_l2_depth: int = Field(default=2, ge=0)
    retrieve_limit: int = Field(default=10, ge=1, le=100)
    adopt_min_score: float = Field(default=0.3, ge=0.0, le=1.0)

    # ── Dedup ──
    dedup_similarity: float = Field(default=0.55, ge=0.0, le=1.0)
    dedup_max_items: int = Field(default=10, ge=1)
    dedup_log: str = ""  # override dedup log path; empty = auto-derive from data_path

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

    # ── Log rotation ──
    log_rotate_mb: float = Field(default=5.0, ge=0.0)  # 0 = disable
    log_rotate_keep: int = Field(default=3, ge=1, le=20)

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
