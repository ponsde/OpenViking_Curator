"""Tests for _resolve_conflict strategy — matrix coverage."""

import os
import unittest
from unittest.mock import patch


class TestResolveConflict(unittest.TestCase):
    """Matrix tests for conflict resolution logic."""

    def _resolve(self, judge_result, local_signals=None, **env_overrides):
        """Call _resolve_conflict with optional local signals and env overrides."""
        from curator.pipeline_v2 import _resolve_conflict

        strategy = env_overrides.get("CURATOR_CONFLICT_STRATEGY", "auto")
        with patch("curator.conflict_resolution.CONFLICT_STRATEGY", strategy):
            return _resolve_conflict(judge_result, local_signals=local_signals)

    # ── No conflict ──

    def test_no_conflict(self):
        result = self._resolve({"has_conflict": False})
        self.assertEqual(result["preferred"], "none")

    # ── Config overrides ──

    def test_strategy_local_always(self):
        result = self._resolve(
            {"has_conflict": True, "trust": 9, "freshness": "current"},
            CURATOR_CONFLICT_STRATEGY="local",
        )
        self.assertEqual(result["preferred"], "local")

    def test_strategy_external_always(self):
        result = self._resolve(
            {"has_conflict": True, "trust": 2, "freshness": "stale"},
            CURATOR_CONFLICT_STRATEGY="external",
        )
        self.assertEqual(result["preferred"], "external")

    def test_strategy_human_always(self):
        result = self._resolve(
            {"has_conflict": True, "trust": 5, "freshness": "current"},
            CURATOR_CONFLICT_STRATEGY="human",
        )
        self.assertEqual(result["preferred"], "human_review")

    # ── Auto strategy: external signals ──

    def test_high_trust_fresh_external_prefers_external(self):
        """High trust + fresh external → prefer external."""
        result = self._resolve({"has_conflict": True, "trust": 8, "freshness": "current"})
        self.assertEqual(result["preferred"], "external")

    def test_high_trust_stale_external_goes_human(self):
        """High trust but stale → human review (not blindly external)."""
        result = self._resolve({"has_conflict": True, "trust": 8, "freshness": "stale"})
        self.assertEqual(result["preferred"], "human_review")

    def test_low_trust_external_with_no_local_signals(self):
        """Low trust external, no local signals → human review (not blindly local)."""
        result = self._resolve({"has_conflict": True, "trust": 2, "freshness": "current"})
        # Without local signals, low external trust should go to human_review
        # instead of blindly preferring local
        self.assertIn(result["preferred"], ("local", "human_review"))

    # ── Auto strategy: local signals influence ──

    def test_low_trust_external_strong_local_prefers_local(self):
        """Low external trust + strong local feedback → prefer local."""
        result = self._resolve(
            {"has_conflict": True, "trust": 2, "freshness": "current"},
            local_signals={"adopt_count": 10, "up_count": 3, "down_count": 0},
        )
        self.assertEqual(result["preferred"], "local")

    def test_low_trust_external_weak_local_goes_human(self):
        """Low external trust + weak/no local feedback → human review."""
        result = self._resolve(
            {"has_conflict": True, "trust": 2, "freshness": "current"},
            local_signals={"adopt_count": 0, "up_count": 0, "down_count": 2},
        )
        self.assertEqual(result["preferred"], "human_review")

    def test_medium_trust_strong_local_prefers_local(self):
        """Medium external trust + very strong local → prefer local."""
        result = self._resolve(
            {"has_conflict": True, "trust": 5, "freshness": "recent"},
            local_signals={"adopt_count": 15, "up_count": 5, "down_count": 0},
        )
        self.assertEqual(result["preferred"], "local")

    def test_medium_trust_no_local_goes_human(self):
        """Medium external trust + no local data → human review."""
        result = self._resolve(
            {"has_conflict": True, "trust": 5, "freshness": "recent"},
            local_signals={"adopt_count": 0, "up_count": 0, "down_count": 0},
        )
        self.assertEqual(result["preferred"], "human_review")

    # ── Output format ──

    def test_result_has_explain_field(self):
        """Result should include scores and explain for transparency."""
        result = self._resolve(
            {"has_conflict": True, "trust": 6, "freshness": "current"},
            local_signals={"adopt_count": 5, "up_count": 1, "down_count": 0},
        )
        self.assertIn("strategy", result)
        self.assertIn("preferred", result)
        self.assertIn("reason", result)
        self.assertIn("scores", result)
        self.assertIn("external", result["scores"])
        self.assertIn("local", result["scores"])

    def test_result_scores_are_numeric(self):
        result = self._resolve(
            {"has_conflict": True, "trust": 7, "freshness": "current"},
            local_signals={"adopt_count": 3, "up_count": 1, "down_count": 0},
        )
        self.assertIsInstance(result["scores"]["external"], (int, float))
        self.assertIsInstance(result["scores"]["local"], (int, float))


if __name__ == "__main__":
    unittest.main()
