"""Tests for KnowledgeBackend abstract interface and OpenVikingBackend."""
import pytest
from curator.backend import KnowledgeBackend, SearchResult, SearchResponse


class DummyBackend(KnowledgeBackend):
    """Minimal backend for testing the interface contract."""

    def __init__(self):
        self._store = {}

    def health(self):
        return True

    def find(self, query, limit=10):
        results = [
            SearchResult(uri=uri, abstract=content[:100], score=0.8)
            for uri, content in self._store.items()
            if query.lower() in content.lower()
        ][:limit]
        return SearchResponse(results=results, total=len(results))

    def search(self, query, limit=10, session_id=None):
        return self.find(query, limit=limit)

    def abstract(self, uri):
        return self._store.get(uri, "")[:100]

    def overview(self, uri):
        return self._store.get(uri, "")[:500]

    def read(self, uri):
        return self._store.get(uri, "")

    def ingest(self, content, title="", metadata=None):
        uri = f"test://{title or 'untitled'}"
        self._store[uri] = content
        return uri


class TestBackendInterface:
    def test_dummy_implements_interface(self):
        backend = DummyBackend()
        assert isinstance(backend, KnowledgeBackend)

    def test_health(self):
        assert DummyBackend().health() is True

    def test_ingest_and_find(self):
        b = DummyBackend()
        uri = b.ingest("Docker deploy Redis full guide", title="docker-redis")
        assert uri == "test://docker-redis"

        r = b.find("docker")
        assert r.total >= 1
        assert r.results[0].uri == uri
        assert r.results[0].score > 0

    def test_read_levels(self):
        b = DummyBackend()
        b.ingest("x" * 1000, title="big")
        assert len(b.abstract("test://big")) <= 100
        assert len(b.overview("test://big")) <= 500
        assert len(b.read("test://big")) == 1000

    def test_search_delegates_to_find(self):
        b = DummyBackend()
        b.ingest("hello world", title="hw")
        r1 = b.find("hello")
        r2 = b.search("hello")
        assert r1.total == r2.total

    def test_empty_search(self):
        b = DummyBackend()
        r = b.find("nonexistent")
        assert r.total == 0
        assert r.results == []

    def test_optional_methods_have_defaults(self):
        b = DummyBackend()
        assert b.supports_sessions is False
        assert b.supports_llm_search is False
        assert b.delete("test://x") is False
        assert b.list_resources() == []
        assert b.create_session() == ""

    def test_search_result_dataclass(self):
        r = SearchResult(uri="test://1", abstract="hello", score=0.9)
        assert r.uri == "test://1"
        assert r.metadata == {}
        assert r.relations == []

    def test_search_response_dataclass(self):
        results = [SearchResult(uri="a"), SearchResult(uri="b")]
        resp = SearchResponse(results=results, total=2)
        assert len(resp.results) == 2
        assert resp.query_plan is None


class TestConflictResolution:
    def test_no_conflict(self):
        from curator.pipeline_v2 import _resolve_conflict
        r = _resolve_conflict({"has_conflict": False})
        assert r["strategy"] == "no_conflict"

    def test_high_trust_external(self):
        from curator.pipeline_v2 import _resolve_conflict
        r = _resolve_conflict({"has_conflict": True, "trust": 8, "freshness": "current"})
        assert r["preferred"] == "external"

    def test_low_trust_local(self):
        from curator.pipeline_v2 import _resolve_conflict
        r = _resolve_conflict({"has_conflict": True, "trust": 2, "freshness": "current"})
        assert r["preferred"] == "local"

    def test_medium_trust_human(self):
        from curator.pipeline_v2 import _resolve_conflict
        r = _resolve_conflict({"has_conflict": True, "trust": 5, "freshness": "recent"})
        assert r["preferred"] == "human_review"
