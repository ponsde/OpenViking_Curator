"""Tests for KnowledgeBackend abstract interface, InMemoryBackend, and JudgeResult."""
import json
import pytest
from curator.backend import KnowledgeBackend, SearchResult, SearchResponse
from curator.backend_memory import InMemoryBackend
from curator.review import JudgeResult, _parse_judge_output


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


class TestInMemoryBackend:
    """Tests for the InMemoryBackend used in testing."""

    def test_implements_interface(self):
        b = InMemoryBackend()
        assert isinstance(b, KnowledgeBackend)

    def test_health(self):
        assert InMemoryBackend().health() is True

    def test_name(self):
        assert InMemoryBackend().name == "InMemory"

    def test_supports_sessions(self):
        assert InMemoryBackend().supports_sessions is True

    def test_ingest_and_find(self):
        b = InMemoryBackend()
        uri = b.ingest("Docker compose deployment guide for production", title="docker-compose")
        resp = b.find("docker")
        assert resp.total >= 1
        assert any(r.uri == uri for r in resp.results)

    def test_ingest_and_read(self):
        b = InMemoryBackend()
        content = "Full document content here " * 20
        uri = b.ingest(content, title="doc")
        assert b.read(uri) == content
        assert len(b.abstract(uri)) <= 100
        assert len(b.overview(uri)) <= 500

    def test_delete(self):
        b = InMemoryBackend()
        uri = b.ingest("to delete", title="del")
        assert b.delete(uri) is True
        assert b.read(uri) == ""
        assert b.delete(uri) is False

    def test_list_resources(self):
        b = InMemoryBackend()
        b.ingest("a", title="alpha")
        b.ingest("b", title="beta")
        uris = b.list_resources()
        assert len(uris) == 2

    def test_list_resources_prefix(self):
        b = InMemoryBackend()
        b.ingest("a", title="alpha")
        b.ingest("b", title="beta")
        uris = b.list_resources(prefix="mem://alpha")
        assert len(uris) == 1

    def test_session_lifecycle(self):
        b = InMemoryBackend()
        sid = b.create_session()
        assert sid.startswith("memsess-")
        b.session_add_message(sid, "user", "hello")
        b.session_add_message(sid, "assistant", "hi")
        b.session_used(sid, ["mem://doc1"])
        result = b.session_commit(sid)
        assert result["active_count_updated"] == 1

    def test_search_returns_searchresponse(self):
        b = InMemoryBackend()
        b.ingest("Python asyncio tutorial", title="asyncio")
        resp = b.search("asyncio")
        assert isinstance(resp, SearchResponse)
        assert resp.total >= 1
        assert all(isinstance(r, SearchResult) for r in resp.results)

    def test_find_empty(self):
        b = InMemoryBackend()
        resp = b.find("nonexistent")
        assert resp.total == 0
        assert resp.results == []

    def test_wait_indexed_noop(self):
        b = InMemoryBackend()
        b.wait_indexed()  # Should not raise

    def test_duplicate_uri_handling(self):
        b = InMemoryBackend()
        uri1 = b.ingest("first", title="same")
        uri2 = b.ingest("second", title="same")
        assert uri1 != uri2
        assert b.read(uri1) == "first"
        assert b.read(uri2) == "second"


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


class TestJudgeResult:
    """Tests for the JudgeResult Pydantic model."""

    def test_basic_construction(self):
        jr = JudgeResult(**{"pass": True, "reason": "good", "trust": 8})
        assert jr.passed is True
        assert jr.reason == "good"
        assert jr.trust == 8

    def test_alias_pass(self):
        """'pass' alias should populate 'passed' field."""
        jr = JudgeResult.model_validate({"pass": True, "reason": "ok"})
        assert jr.passed is True

    def test_defaults(self):
        jr = JudgeResult()
        assert jr.passed is False
        assert jr.trust == 0
        assert jr.freshness == "unknown"
        assert jr.conflict_points == []

    def test_trust_bounds(self):
        with pytest.raises(Exception):
            JudgeResult(trust=11)
        with pytest.raises(Exception):
            JudgeResult(trust=-1)

    def test_freshness_validation(self):
        jr = JudgeResult(freshness="current")
        assert jr.freshness == "current"
        with pytest.raises(Exception):
            JudgeResult(freshness="invalid")

    def test_to_pipeline_dict(self):
        jr = JudgeResult(**{
            "pass": True, "reason": "good", "trust": 7,
            "freshness": "current", "markdown": "# Doc",
            "has_conflict": True, "conflict_summary": "differs",
            "conflict_points": ["point1"],
        })
        d = jr.to_pipeline_dict()
        assert d["pass"] is True
        assert d["trust"] == 7
        assert d["conflict_points"] == ["point1"]
        # Key is "pass" not "passed"
        assert "passed" not in d

    def test_model_validate_json(self):
        raw = json.dumps({
            "pass": True, "reason": "ok", "trust": 6,
            "freshness": "recent", "summary": "s", "markdown": "m",
            "has_conflict": False, "conflict_summary": "",
            "conflict_points": [],
        })
        jr = JudgeResult.model_validate_json(raw)
        assert jr.passed is True
        assert jr.trust == 6

    def test_parse_judge_output_valid(self):
        raw = 'Here is the result:\n{"pass": true, "reason": "good", "trust": 8, "freshness": "current", "summary": "x", "markdown": "y", "has_conflict": false, "conflict_summary": "", "conflict_points": []}'
        jr = _parse_judge_output(raw)
        assert jr.passed is True
        assert jr.trust == 8

    def test_parse_judge_output_none(self):
        jr = _parse_judge_output(None, fallback_reason="timeout")
        assert jr.passed is False
        assert jr.reason == "timeout"

    def test_parse_judge_output_bad_json(self):
        jr = _parse_judge_output("no json here")
        assert jr.passed is False
        assert jr.reason == "bad_json"

    def test_parse_judge_output_partial_json(self):
        """Should handle JSON with missing optional fields gracefully."""
        raw = '{"pass": true, "reason": "ok", "trust": 5}'
        jr = _parse_judge_output(raw)
        assert jr.passed is True
        assert jr.freshness == "unknown"
        assert jr.conflict_points == []
