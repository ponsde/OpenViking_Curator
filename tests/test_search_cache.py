"""Tests for curator.search_cache — external search result caching."""

import json
import os
import time

import pytest


@pytest.fixture(autouse=True)
def _cache_env(tmp_path, monkeypatch):
    """Enable cache and point DATA_PATH to tmp_path for every test."""
    monkeypatch.setattr("curator.search_cache.CACHE_ENABLED", True)
    monkeypatch.setattr("curator.search_cache.CACHE_TTL", 3600)
    monkeypatch.setattr("curator.search_cache.CACHE_FRESH_TTL", 300)
    monkeypatch.setattr("curator.search_cache.CACHE_MAX_ENTRIES", 200)
    monkeypatch.setattr("curator.search_cache.DATA_PATH", str(tmp_path))


class TestCacheDisabled:
    def test_get_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr("curator.search_cache.CACHE_ENABLED", False)
        from curator.search_cache import get

        assert get("query", {"domain": ""}) is None

    def test_put_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr("curator.search_cache.CACHE_ENABLED", False)
        from curator.search_cache import put, stats

        put("query", {"domain": ""}, "text")
        assert stats()["entries"] == 0


class TestPutGet:
    def test_basic_put_get(self):
        from curator.search_cache import get, put

        scope = {"domain": "devops"}
        put("how to deploy redis", scope, "Redis deployment guide...")
        result = get("how to deploy redis", scope)
        assert result == "Redis deployment guide..."

    def test_miss_returns_none(self):
        from curator.search_cache import get

        assert get("nonexistent query", {"domain": ""}) is None

    def test_empty_text_not_stored(self):
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("q", scope, "")
        assert get("q", scope) is None
        put("q", scope, "   ")
        assert get("q", scope) is None


class TestTTL:
    def test_expired_entry_returns_none(self, monkeypatch):
        import curator.search_cache as sc
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("q", scope, "data")
        # fast-forward time beyond TTL
        future = time.time() + 3700
        monkeypatch.setattr(sc.time, "time", lambda: future)
        assert get("q", scope) is None

    def test_fresh_ttl_shorter(self, monkeypatch):
        import curator.search_cache as sc
        from curator.search_cache import get, put

        scope = {"domain": "", "need_fresh": True}
        put("q", scope, "fresh data")
        # Still within normal TTL but beyond fresh TTL
        future = time.time() + 400
        monkeypatch.setattr(sc.time, "time", lambda: future)
        assert get("q", scope) is None

    def test_normal_ttl_within_range(self, monkeypatch):
        import curator.search_cache as sc
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("q", scope, "still valid")
        # Within normal TTL
        future = time.time() + 1800
        monkeypatch.setattr(sc.time, "time", lambda: future)
        assert get("q", scope) == "still valid"


class TestKeyNormalization:
    def test_case_insensitive(self):
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("How to Deploy Redis", scope, "guide")
        assert get("how to deploy redis", scope) == "guide"

    def test_whitespace_stripped(self):
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("  query  ", scope, "data")
        assert get("query", scope) == "data"


class TestDomainSeparation:
    def test_different_domains_separate(self):
        from curator.search_cache import get, put

        put("redis", {"domain": "devops"}, "devops redis")
        put("redis", {"domain": "backend"}, "backend redis")
        assert get("redis", {"domain": "devops"}) == "devops redis"
        assert get("redis", {"domain": "backend"}) == "backend redis"


class TestLRUEviction:
    def test_eviction_removes_oldest(self, monkeypatch):
        monkeypatch.setattr("curator.search_cache.CACHE_MAX_ENTRIES", 3)
        from curator.search_cache import get, put

        scope = {"domain": ""}
        put("q1", scope, "d1")
        put("q2", scope, "d2")
        put("q3", scope, "d3")
        # This should evict q1 (oldest last_access)
        put("q4", scope, "d4")
        assert get("q1", scope) is None
        assert get("q4", scope) == "d4"


class TestClearAndStats:
    def test_clear_removes_all(self):
        from curator.search_cache import clear, get, put, stats

        put("q", {"domain": ""}, "data")
        assert stats()["entries"] == 1
        clear()
        assert stats()["entries"] == 0
        assert get("q", {"domain": ""}) is None

    def test_stats_empty(self):
        from curator.search_cache import stats

        s = stats()
        assert s["entries"] == 0


class TestPipelineIntegration:
    def test_cache_hit_skips_external(self, monkeypatch, tmp_path):
        """When cache hits, pipeline should not call external_search."""
        from unittest.mock import MagicMock

        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        backend.ingest("Redis is a cache server", title="redis", metadata={})

        # Mock config.chat so no real LLM calls
        monkeypatch.setattr("curator.config.chat", lambda *a, **kw: '{"passed":true,"trust":7}')
        monkeypatch.setattr("curator.config.validate_config", lambda: None)

        # Point search_cache DATA_PATH to tmp_path (affects the module pipeline imports)
        monkeypatch.setattr("curator.search_cache.DATA_PATH", str(tmp_path))

        # Pre-populate cache
        import curator.search_cache as search_cache

        search_cache.put("redis deploy", {"domain": "devops"}, "cached external result")

        # Track if external_search is called
        calls = []

        def mock_search(q, s):
            calls.append(q)
            return "should not be called"

        monkeypatch.setattr("curator.pipeline_v2.external_search", mock_search)

        # Force external trigger by lowering coverage
        monkeypatch.setattr(
            "curator.pipeline_v2.assess_coverage",
            lambda *a, **kw: (0.1, True, "low_coverage"),
        )

        from curator.pipeline_v2 import _run_impl

        mock_ov = MagicMock()
        mock_ov.health.return_value = True
        mock_sm = MagicMock()

        monkeypatch.setattr("curator.pipeline_v2.route_scope", lambda q: {"domain": "devops"})

        result = _run_impl("redis deploy", backend, True, lambda: (mock_ov, mock_sm))

        # external_search should NOT have been called because cache hit
        assert len(calls) == 0
        assert result["external_text"] == "cached external result"
