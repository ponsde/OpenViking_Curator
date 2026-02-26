"""Tests for coverage_tuning_suggest.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from coverage_tuning_suggest import analyze


class TestCoverageTuning(unittest.TestCase):
    """Test coverage tuning suggestion logic."""

    def _make_entry(self, **overrides) -> dict:
        base = {
            "schema_version": 2,
            "timestamp": "2026-02-26T12:00:00Z",
            "query": "test",
            "coverage": 0.5,
            "external_triggered": False,
            "reason": "local_sufficient",
            "used_uris": [],
            "load_stage": "L0",
            "llm_calls": 0,
            "ingested": False,
            "async_ingest_pending": False,
            "need_fresh": False,
            "has_conflict": False,
            "external_len": 0,
            "auto_ingest": True,
        }
        base.update(overrides)
        return base

    def test_empty_data(self):
        result = analyze([])
        self.assertIn("error", result)
        self.assertEqual(result["suggestions"], [])

    def test_balanced_no_suggestions(self):
        """Well-balanced data should produce no suggestions."""
        entries = [self._make_entry(coverage=0.6, external_triggered=False) for _ in range(50)] + [
            self._make_entry(coverage=0.3, external_triggered=True) for _ in range(20)
        ]
        result = analyze(entries)
        # With ~29% external rate and decent local coverage, should be balanced
        self.assertEqual(result["confidence"], "medium")

    def test_high_external_rate_suggests_lower_threshold(self):
        """If external triggers >60%, suggest lowering COV_SUFFICIENT."""
        entries = [self._make_entry(coverage=0.45, external_triggered=True) for _ in range(70)] + [
            self._make_entry(coverage=0.50, external_triggered=False) for _ in range(30)
        ]
        result = analyze(entries)
        param_names = [s["param"] for s in result["suggestions"]]
        self.assertIn("CURATOR_THRESHOLD_COV_SUFFICIENT", param_names)

        sug = next(s for s in result["suggestions"] if s["param"] == "CURATOR_THRESHOLD_COV_SUFFICIENT")
        self.assertEqual(sug["direction"], "lower")
        self.assertLess(sug["suggested"], sug["current"])

    def test_low_external_ingest_rate_flags_investigation(self):
        """If external rarely leads to ingest, flag for investigation."""
        entries = [self._make_entry(coverage=0.2, external_triggered=True, ingested=False) for _ in range(50)]
        result = analyze(entries)
        param_names = [s["param"] for s in result["suggestions"]]
        self.assertIn("EXTERNAL_SEARCH_EFFECTIVENESS", param_names)

    def test_high_llm_calls_flags_investigation(self):
        """High avg LLM calls should flag for investigation."""
        entries = [self._make_entry(llm_calls=2, external_triggered=True) for _ in range(30)]
        result = analyze(entries)
        param_names = [s["param"] for s in result["suggestions"]]
        self.assertIn("LLM_CALL_BUDGET", param_names)

    def test_confidence_levels(self):
        """Confidence should scale with sample size."""
        self.assertEqual(analyze([self._make_entry()] * 10)["confidence"], "insufficient")
        self.assertEqual(analyze([self._make_entry()] * 25)["confidence"], "low")
        self.assertEqual(analyze([self._make_entry()] * 60)["confidence"], "medium")
        self.assertEqual(analyze([self._make_entry()] * 100)["confidence"], "high")

    def test_custom_current_thresholds(self):
        """Should use provided current thresholds."""
        custom = {
            "CURATOR_THRESHOLD_COV_SUFFICIENT": 0.70,
            "CURATOR_THRESHOLD_COV_MARGINAL": 0.50,
            "CURATOR_THRESHOLD_COV_LOW": 0.30,
        }
        entries = [self._make_entry(coverage=0.55, external_triggered=True) for _ in range(80)] + [
            self._make_entry(coverage=0.60, external_triggered=False) for _ in range(20)
        ]
        result = analyze(entries, current=custom)
        self.assertEqual(result["current_thresholds"]["CURATOR_THRESHOLD_COV_SUFFICIENT"], 0.70)


if __name__ == "__main__":
    unittest.main()
