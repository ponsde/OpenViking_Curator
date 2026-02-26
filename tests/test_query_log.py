"""Tests for query_log schema v2 and aggregation."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from query_log_aggregate import aggregate, load_entries


class TestQueryLogSchema(unittest.TestCase):
    """Verify _log_query writes schema v2 entries with all required fields."""

    REQUIRED_FIELDS_V2 = {
        "schema_version",
        "timestamp",
        "query",
        "coverage",
        "external_triggered",
        "reason",
        "used_uris",
        "load_stage",
        "llm_calls",
        "ingested",
        "async_ingest_pending",
        "need_fresh",
        "has_conflict",
        "external_len",
        "auto_ingest",
    }

    def test_log_query_writes_v2_schema(self):
        """Pipeline _log_query should write all v2 fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("curator.pipeline_v2.DATA_PATH", tmpdir):
                from curator.pipeline_v2 import _log_query

                _log_query(
                    "test query",
                    0.65,
                    True,
                    "low_coverage",
                    ["uri://a"],
                    {"load_stage": "L1", "llm_calls": 1},
                    ingested=True,
                    async_ingest_pending=False,
                    need_fresh=True,
                    has_conflict=False,
                    external_len=500,
                    auto_ingest=True,
                )

            log_path = Path(tmpdir) / "query_log.jsonl"
            self.assertTrue(log_path.exists())
            entry = json.loads(log_path.read_text().strip())

            self.assertEqual(entry["schema_version"], 2)
            missing = self.REQUIRED_FIELDS_V2 - set(entry.keys())
            self.assertEqual(missing, set(), f"Missing fields: {missing}")

            # Type checks
            self.assertIsInstance(entry["coverage"], float)
            self.assertIsInstance(entry["external_triggered"], bool)
            self.assertIsInstance(entry["ingested"], bool)
            self.assertIsInstance(entry["external_len"], int)

    def test_log_query_defaults(self):
        """_log_query with defaults should still produce valid v2 entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("curator.pipeline_v2.DATA_PATH", tmpdir):
                from curator.pipeline_v2 import _log_query

                _log_query("default query", 0.0, False, "no_results", [], {"load_stage": "none", "llm_calls": 0})

            log_path = Path(tmpdir) / "query_log.jsonl"
            entry = json.loads(log_path.read_text().strip())
            self.assertEqual(entry["schema_version"], 2)
            self.assertFalse(entry["ingested"])
            self.assertFalse(entry["need_fresh"])
            self.assertEqual(entry["external_len"], 0)


class TestQueryLogAggregate(unittest.TestCase):
    """Tests for query_log_aggregate.py functions."""

    def _make_entries(self, n: int, **overrides) -> list[dict]:
        """Generate n synthetic log entries."""
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
        return [dict(base) for _ in range(n)]

    def test_empty_input(self):
        """Empty entries should return error."""
        result = aggregate([])
        self.assertEqual(result["total_queries"], 0)
        self.assertIn("error", result)

    def test_basic_aggregate(self):
        """Basic aggregation with uniform entries."""
        entries = self._make_entries(10, coverage=0.6, external_triggered=True)
        result = aggregate(entries)

        self.assertEqual(result["total_queries"], 10)
        self.assertAlmostEqual(result["coverage"]["mean"], 0.6, places=2)
        self.assertAlmostEqual(result["rates"]["external_triggered"], 1.0)
        self.assertAlmostEqual(result["rates"]["ingested"], 0.0)

    def test_mixed_entries(self):
        """Mixed entries produce correct rates."""
        entries = self._make_entries(
            3, external_triggered=True, ingested=True, has_conflict=False
        ) + self._make_entries(7, external_triggered=False, ingested=False, has_conflict=False)
        result = aggregate(entries)

        self.assertEqual(result["total_queries"], 10)
        self.assertAlmostEqual(result["rates"]["external_triggered"], 0.3, places=2)
        self.assertAlmostEqual(result["rates"]["ingested"], 0.3, places=2)

    def test_coverage_percentiles(self):
        """Coverage percentiles should be ordered."""
        entries = [dict(self._make_entries(1, coverage=c / 10.0)[0]) for c in range(1, 11)]
        result = aggregate(entries)

        self.assertLessEqual(result["coverage"]["p25"], result["coverage"]["p50"])
        self.assertLessEqual(result["coverage"]["p50"], result["coverage"]["p75"])
        self.assertLessEqual(result["coverage"]["p75"], result["coverage"]["p90"])

    def test_reason_breakdown(self):
        """Coverage reasons should sum to total."""
        entries = self._make_entries(5, reason="low_coverage") + self._make_entries(3, reason="local_sufficient")
        result = aggregate(entries)

        total_reasons = sum(result["coverage_reasons"].values())
        self.assertEqual(total_reasons, 8)

    def test_load_entries_skips_malformed(self):
        """Malformed lines should be skipped gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"query": "good", "coverage": 0.5}\n')
            f.write("not json\n")
            f.write('{"no_query_field": true}\n')
            f.write('{"query": "also good", "coverage": 0.8}\n')
            f.name

        try:
            entries = load_entries(Path(f.name))
            self.assertEqual(len(entries), 2)
        finally:
            os.unlink(f.name)

    def test_schema_version_distribution(self):
        """Aggregate should report schema version distribution."""
        v1 = [{"query": "old", "coverage": 0.5}]  # no schema_version → counted as "1"
        v2 = self._make_entries(3)
        result = aggregate(v1 + v2)
        self.assertIn("1", result["schema_versions"])
        self.assertIn("2", result["schema_versions"])


if __name__ == "__main__":
    unittest.main()
