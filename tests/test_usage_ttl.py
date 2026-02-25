"""
tests/test_usage_ttl.py — Unit tests for usage-based TTL adjustment.
"""
import json
import os
import pytest
import tempfile


# ─── usage_tier ───────────────────────────────────────────────────────────────

class TestUsageTier:
    def test_hot_at_threshold(self):
        from curator.usage_ttl import usage_tier
        assert usage_tier(5) == "hot"

    def test_hot_above_threshold(self):
        from curator.usage_ttl import usage_tier
        assert usage_tier(10) == "hot"
        assert usage_tier(100) == "hot"

    def test_warm_lower_bound(self):
        from curator.usage_ttl import usage_tier
        assert usage_tier(1) == "warm"

    def test_warm_upper_bound(self):
        from curator.usage_ttl import usage_tier
        assert usage_tier(4) == "warm"

    def test_cold_zero(self):
        from curator.usage_ttl import usage_tier
        assert usage_tier(0) == "cold"

    def test_cold_negative_treated_as_zero(self):
        from curator.usage_ttl import usage_tier
        # Should not raise; negative counts treated as cold
        assert usage_tier(-1) == "cold"


# ─── adjust_ttl ───────────────────────────────────────────────────────────────

class TestAdjustTtl:
    def test_warm_unchanged(self):
        from curator.usage_ttl import adjust_ttl
        assert adjust_ttl(180, "warm") == 180
        assert adjust_ttl(60, "warm") == 60

    def test_hot_multiplier(self):
        from curator.usage_ttl import adjust_ttl
        assert adjust_ttl(180, "hot") == 270   # 180 * 1.5

    def test_cold_multiplier(self):
        from curator.usage_ttl import adjust_ttl
        assert adjust_ttl(180, "cold") == 90   # 180 * 0.5

    def test_hot_capped_at_365(self):
        from curator.usage_ttl import adjust_ttl
        assert adjust_ttl(300, "hot") == 365   # 300*1.5=450 → cap 365

    def test_cold_floor_nonzero(self):
        from curator.usage_ttl import adjust_ttl
        # cold of 1-day doc → floor 1, not 0
        assert adjust_ttl(1, "cold") >= 1

    def test_outdated_base_stays_zero(self):
        from curator.usage_ttl import adjust_ttl
        # "outdated" freshness maps to ttl_days=0; usage signal shouldn't resurrect it
        assert adjust_ttl(0, "hot") == 0
        assert adjust_ttl(0, "cold") == 0

    def test_unknown_tier_defaults_to_warm(self):
        from curator.usage_ttl import adjust_ttl
        assert adjust_ttl(90, "unknown_tier") == 90  # 1.0x fallback


# ─── compute_usage_ttl_for_ingest ─────────────────────────────────────────────

class TestComputeUsageTtlForIngest:
    def _make_feedback_file(self, data: dict) -> str:
        """Write feedback data to a temp file and return its path."""
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(data, f)
        f.close()
        return f.name

    def test_no_uris_returns_base(self):
        from curator.usage_ttl import compute_usage_ttl_for_ingest
        ttl, tier = compute_usage_ttl_for_ingest(180, [])
        assert ttl == 180
        assert tier == "warm"  # default when no signal

    def test_uris_with_no_feedback_cold(self, monkeypatch, tmp_path):
        fb_file = tmp_path / "feedback.json"
        fb_file.write_text("{}")
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))
        # Reload module to pick up env
        import importlib, curator.usage_ttl as m
        importlib.reload(m)
        ttl, tier = m.compute_usage_ttl_for_ingest(180, ["viking://res/abc"])
        assert tier == "cold"
        assert ttl == 90   # 180 * 0.5

    def test_hot_uri_raises_ttl(self, monkeypatch, tmp_path):
        fb_data = {"viking://res/hot-doc": {"up": 0, "down": 0, "adopt": 7}}
        fb_file = tmp_path / "feedback.json"
        fb_file.write_text(json.dumps(fb_data))
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))
        import importlib, curator.usage_ttl as m
        importlib.reload(m)
        ttl, tier = m.compute_usage_ttl_for_ingest(
            180, ["viking://res/hot-doc", "viking://res/cold-doc"]
        )
        assert tier == "hot"
        assert ttl == 270   # 180 * 1.5

    def test_mixed_uses_max_adopt(self, monkeypatch, tmp_path):
        fb_data = {
            "viking://res/a": {"up": 0, "down": 0, "adopt": 2},
            "viking://res/b": {"up": 0, "down": 0, "adopt": 6},
            "viking://res/c": {"up": 0, "down": 0, "adopt": 0},
        }
        fb_file = tmp_path / "feedback.json"
        fb_file.write_text(json.dumps(fb_data))
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))
        import importlib, curator.usage_ttl as m
        importlib.reload(m)
        ttl, tier = m.compute_usage_ttl_for_ingest(
            180, ["viking://res/a", "viking://res/b", "viking://res/c"]
        )
        # max adopt = 6 → hot
        assert tier == "hot"
        assert ttl == 270


# ─── ingest_markdown_v2 integration ───────────────────────────────────────────

class TestIngestMarkdownV2UsageTtl:
    """Verify that ingest_markdown_v2 adjusts ttl_days when uri_hints provided."""

    def test_hot_hints_increase_ttl(self, monkeypatch, tmp_path):
        import json as _json
        from curator.backend_memory import InMemoryBackend

        # Set up feedback file with a hot URI
        fb_data = {"viking://res/popular": {"up": 0, "down": 0, "adopt": 8}}
        fb_file = tmp_path / "feedback.json"
        fb_file.write_text(_json.dumps(fb_data))
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))
        monkeypatch.setenv("CURATOR_CURATED_DIR", str(tmp_path / "curated"))

        import importlib
        import curator.usage_ttl as ut
        import curator.review as rv
        importlib.reload(ut)
        importlib.reload(rv)

        backend = InMemoryBackend()
        result = rv.ingest_markdown_v2(
            backend, "Test doc", "# Hello\n\nContent.",
            freshness="current",
            uri_hints=["viking://res/popular"],
        )
        assert "root_uri" in result
        # ttl_days in stored metadata should be 270 (180 * 1.5), not 180
        stored = backend._store[result["root_uri"]]
        assert stored["metadata"]["ttl_days"] == 270
        assert stored["metadata"]["usage_tier"] == "hot"

    def test_no_hints_uses_base_ttl(self, monkeypatch, tmp_path):
        from curator.backend_memory import InMemoryBackend
        monkeypatch.setenv("CURATOR_CURATED_DIR", str(tmp_path / "curated"))

        import importlib
        import curator.review as rv
        importlib.reload(rv)

        backend = InMemoryBackend()
        result = rv.ingest_markdown_v2(
            backend, "Test doc 2", "# Hello\n\nContent.",
            freshness="current",
        )
        stored = backend._store[result["root_uri"]]
        assert stored["metadata"]["ttl_days"] == 180   # unchanged
