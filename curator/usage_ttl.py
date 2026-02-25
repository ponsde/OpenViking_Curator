"""Usage-based TTL adjustment for ingested documents.

Instead of purely static freshness-based TTL (current=180d, recent=90d …),
this module adjusts TTL based on how frequently a topic's documents are
actually *adopted* (retrieved and used) by the pipeline.

Tier classification (based on adopt count of existing topic URIs):
  hot  (adopt >= 5) → TTL × 1.5  — popular topic, keep longer
  warm (adopt 1–4)  → TTL × 1.0  — normal use
  cold (adopt = 0)  → TTL × 0.5  — never used, shrink TTL

The adjust_ttl() function:
  - Preserves TTL = 0 (outdated docs stay at 0 regardless of tier).
  - Caps at 365 days maximum.
  - Applies a floor of 1 day for non-zero base TTLs after cold adjustment.

Usage in pipeline (compute_usage_ttl_for_ingest):
  When deciding TTL for a *new* ingest, we look at the adopt signals of
  *existing* documents retrieved for the same query. This is a reasonable
  proxy: if existing docs on this topic are well-adopted, the new content is
  on a hot topic and deserves a longer TTL. If they've never been used, cold.
"""

from __future__ import annotations

# ── Constants ──────────────────────────────────────────────────────────────────

_HOT_THRESHOLD = 5       # adopt count to qualify as hot
_MULTIPLIERS = {
    "hot":  1.5,
    "warm": 1.0,
    "cold": 0.5,
}
_MAX_TTL = 365


# ── Core functions ─────────────────────────────────────────────────────────────

def usage_tier(adopt_count: int) -> str:
    """Classify adopt_count into a tier string.

    Args:
        adopt_count: Number of times the resource has been adopted by the
                     pipeline. Negative values are treated as zero.

    Returns:
        ``"hot"``, ``"warm"``, or ``"cold"``.
    """
    count = max(0, adopt_count)
    if count >= _HOT_THRESHOLD:
        return "hot"
    if count >= 1:
        return "warm"
    return "cold"


def adjust_ttl(base_ttl: int, tier: str) -> int:
    """Apply tier multiplier to base_ttl.

    Special cases:
      - base_ttl == 0 ("outdated") → always returns 0 regardless of tier.
      - result > 0 but < 1 after rounding → floor to 1.
      - result > 365 → capped at 365.

    Args:
        base_ttl: Freshness-based TTL in days (0–365).
        tier: One of ``"hot"``, ``"warm"``, ``"cold"``. Unknown values
              default to the warm multiplier (1.0, i.e. no change).

    Returns:
        Adjusted TTL in days (int).
    """
    if base_ttl <= 0:
        return 0

    multiplier = _MULTIPLIERS.get(tier, _MULTIPLIERS["warm"])
    adjusted = base_ttl * multiplier

    # Floor non-zero results to at least 1
    adjusted = max(1.0, adjusted)
    # Cap at maximum
    adjusted = min(_MAX_TTL, adjusted)

    return round(adjusted)


# ── Pipeline helper ────────────────────────────────────────────────────────────

def compute_usage_ttl_for_ingest(
    base_ttl: int,
    existing_uris: list,
) -> tuple[int, str]:
    """Compute adjusted TTL for a new ingest, informed by existing topic URIs.

    Reads the feedback store once, finds the maximum adopt count among the
    provided URIs, classifies that into a tier, and returns the adjusted TTL.

    When *existing_uris* is empty (no local results), the function returns
    (base_ttl, "warm") — no adjustment, conservative default.

    When *existing_uris* is non-empty but no feedback signals are found for
    any URI (all adopt counts = 0), the topic is treated as cold (never used)
    and TTL is halved. This intentionally lets unused topics expire faster.

    Args:
        base_ttl:      Freshness-based TTL in days from ``ingest_markdown_v2``.
        existing_uris: URIs of documents retrieved from OV for the same query
                       (i.e. the local context before external search fired).

    Returns:
        ``(adjusted_ttl, tier)`` tuple.
    """
    if not existing_uris:
        return base_ttl, "warm"

    from .feedback_store import load as _load_feedback

    try:
        data = _load_feedback()
    except Exception:
        return base_ttl, "warm"

    # Find the maximum adopt signal across all provided URIs
    max_adopt = 0
    for uri in existing_uris:
        signals = data.get(uri, {})
        max_adopt = max(max_adopt, signals.get("adopt", 0))

    tier = usage_tier(max_adopt)
    return adjust_ttl(base_ttl, tier), tier
