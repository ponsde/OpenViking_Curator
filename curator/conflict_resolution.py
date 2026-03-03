"""Conflict resolution — bidirectional scoring between external and local knowledge.

Extracted from pipeline_v2.py (D2 refactor). Internal module, not part of public API.
"""

from __future__ import annotations

from typing import Any

from .config import CONFLICT_STRATEGY, log


def _aggregate_local_signals(used_uris: list | set, *, feedback_data: dict | None = None) -> dict | None:
    """Aggregate feedback signals for local URIs used in this run.

    Args:
        used_uris: URIs used in the current pipeline run.
        feedback_data: Pre-loaded feedback dict. When provided, skips the
            internal ``feedback_store.load()`` call. ``None`` preserves the
            original behaviour (load on demand).

    Returns dict with adopt_count, up_count, down_count summed across
    all used URIs. Returns None if feedback_store is unavailable.
    """
    if not used_uris:
        return None

    if feedback_data is not None:
        data = feedback_data
    else:
        try:
            from . import feedback_store

            data = feedback_store.load()
        except Exception as e:
            log.debug("failed to load feedback signals for URIs %s: %s", used_uris, e)
            return None

    adopt = up = down = 0
    for uri in used_uris:
        item = data.get(uri, {})
        adopt += item.get("adopt", 0)
        up += item.get("up", 0)
        down += item.get("down", 0)
    return {"adopt_count": adopt, "up_count": up, "down_count": down}


def _resolve_conflict(judge_result: dict, *, local_signals: dict | None = None) -> dict:
    """Conflict resolution strategy — bidirectional scoring.

    Scores both external and local knowledge to decide which to prefer.
    External score is based on judge trust + freshness.
    Local score is based on feedback signals (adopt/up/down).

    When neither side is clearly stronger, defers to human review.

    Args:
        judge_result: Output from judge_and_ingest (has trust, freshness, etc.)
        local_signals: Optional dict with ``adopt_count``, ``up_count``,
            ``down_count`` from feedback_store. ``None`` means no data.

    Returns:
        Dict with ``strategy``, ``preferred``, ``reason``, and ``scores``.
    """
    no_conflict: dict[str, Any] = {
        "strategy": "no_conflict",
        "preferred": "none",
        "reason": "",
        "scores": {"external": 0, "local": 0},
    }
    if not judge_result.get("has_conflict"):
        return no_conflict

    trust = judge_result.get("trust", 5)
    freshness = judge_result.get("freshness", "unknown")

    strategy = CONFLICT_STRATEGY or "auto"

    if strategy == "local":
        return {
            "strategy": "local_always",
            "preferred": "local",
            "reason": "config: always prefer local",
            "scores": {"external": 0, "local": 0},
        }
    elif strategy == "external":
        return {
            "strategy": "external_always",
            "preferred": "external",
            "reason": "config: always prefer external",
            "scores": {"external": 0, "local": 0},
        }
    elif strategy == "human":
        return {
            "strategy": "human_always",
            "preferred": "human_review",
            "reason": "config: always human review",
            "scores": {"external": 0, "local": 0},
        }

    # ── Score external source ──
    # trust: 0-10 from judge LLM
    # freshness bonus: current=+2, recent=+1, stale=-2, outdated=-3
    freshness_bonus = {"current": 2, "recent": 1, "unknown": 0, "stale": -2, "outdated": -3}
    ext_score = trust + freshness_bonus.get(freshness, 0)

    # ── Score local knowledge ──
    # Based on feedback signals: adopt is strongest (used by pipeline),
    # up/down are explicit user feedback
    local_score = 5.0  # neutral baseline
    if local_signals is not None:
        adopt = local_signals.get("adopt_count", 0)
        up = local_signals.get("up_count", 0)
        down = local_signals.get("down_count", 0)
        # adopt is weighted higher (objective signal from pipeline)
        local_score = 5.0 + min(adopt * 0.3, 3.0) + min(up * 0.5, 2.0) - min(down * 0.7, 3.0)
        local_score = max(0, min(12, local_score))
    else:
        # No feedback data → local score stays at neutral
        local_score = 5.0

    scores = {"external": round(ext_score, 2), "local": round(local_score, 2)}

    # ── Decision ──
    margin = 2.0  # minimum gap to make a confident decision
    diff = ext_score - local_score

    if diff >= margin:
        preferred = "external"
        reason = f"external stronger (ext={ext_score:.1f} vs local={local_score:.1f}, diff={diff:+.1f})"
    elif diff <= -margin:
        preferred = "local"
        reason = f"local stronger (local={local_score:.1f} vs ext={ext_score:.1f}, diff={diff:+.1f})"
    else:
        preferred = "human_review"
        reason = (
            f"scores too close (ext={ext_score:.1f} vs local={local_score:.1f}, diff={diff:+.1f}), needs human judgment"
        )

    return {"strategy": "auto", "preferred": preferred, "reason": reason, "scores": scores}
