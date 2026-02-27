"""Tests for curator.interest_analyzer."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from curator.interest_analyzer import (
    InterestTopic,
    ProactiveQuery,
    extract_interests,
    generate_proactive_queries,
)


def _ts(days_ago: int = 0) -> str:
    """ISO timestamp N days ago."""
    t = time.time() - days_ago * 86400
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _write_query_log(data_dir: str, entries: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "query_log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _write_feedback(feedback: dict, monkeypatch) -> None:
    """Point feedback store at a temp file with given data."""
    import tempfile

    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(feedback, tf, ensure_ascii=False)
    tf.close()
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", tf.name)
    # Also patch the module-level STORE so _resolve_store() picks it up
    from pathlib import Path

    monkeypatch.setattr("curator.feedback_store.STORE", Path(tf.name))


# ── extract_interests ────────────────────────────────────────────────────────


class TestExtractInterests:
    def test_empty_log(self, tmp_path):
        """No query log → empty results."""
        result = extract_interests(data_path=str(tmp_path))
        assert result == []

    def test_single_topic_below_min_queries(self, tmp_path, monkeypatch):
        """Topics with < min_queries are filtered out."""
        _write_feedback({}, monkeypatch)
        _write_query_log(
            str(tmp_path),
            [{"query": "redis cluster", "coverage": 0.3, "timestamp": _ts(1)}],
        )
        result = extract_interests(data_path=str(tmp_path), min_queries=2)
        assert result == []

    def test_basic_scoring(self, tmp_path, monkeypatch):
        """Two topics: high-frequency low-coverage ranks higher."""
        _write_feedback({}, monkeypatch)
        entries = [
            # Topic A: "docker compose" — queried 4 times, low coverage
            {"query": "docker compose setup", "coverage": 0.2, "timestamp": _ts(1)},
            {"query": "docker compose networking", "coverage": 0.3, "timestamp": _ts(2)},
            {"query": "docker compose volumes", "coverage": 0.25, "timestamp": _ts(3)},
            {"query": "docker compose healthcheck", "coverage": 0.2, "timestamp": _ts(5)},
            # Topic B: "python asyncio" — queried 2 times, higher coverage
            {"query": "python asyncio gather", "coverage": 0.7, "timestamp": _ts(1)},
            {"query": "python asyncio wait", "coverage": 0.8, "timestamp": _ts(10)},
        ]
        _write_query_log(str(tmp_path), entries)

        result = extract_interests(data_path=str(tmp_path), min_queries=2)
        assert len(result) >= 2
        # Docker compose should rank higher (more queries, lower coverage)
        docker_topics = [t for t in result if "docker" in t.topic]
        python_topics = [t for t in result if "python" in t.topic]
        assert docker_topics and python_topics
        assert docker_topics[0].interest_score > python_topics[0].interest_score

    def test_adopt_score_boost(self, tmp_path, monkeypatch):
        """Topics whose URIs have high adopt counts score higher."""
        uri_a = "viking://resources/1771327401_topicA"
        uri_b = "viking://resources/1771327402_topicB"
        _write_feedback(
            {
                uri_a: {"up": 0, "down": 0, "adopt": 10},
                uri_b: {"up": 0, "down": 0, "adopt": 0},
            },
            monkeypatch,
        )
        entries = [
            {"query": "redis caching patterns", "coverage": 0.3, "timestamp": _ts(1), "used_uris": [uri_a]},
            {"query": "redis caching best", "coverage": 0.3, "timestamp": _ts(2), "used_uris": [uri_a]},
            {"query": "golang concurrency model", "coverage": 0.3, "timestamp": _ts(1), "used_uris": [uri_b]},
            {"query": "golang concurrency patterns", "coverage": 0.3, "timestamp": _ts(2), "used_uris": [uri_b]},
        ]
        _write_query_log(str(tmp_path), entries)

        result = extract_interests(data_path=str(tmp_path), min_queries=2)
        redis_topics = [t for t in result if "redis" in t.topic]
        go_topics = [t for t in result if "golang" in t.topic]
        assert redis_topics and go_topics
        assert redis_topics[0].adopt_score > go_topics[0].adopt_score
        assert redis_topics[0].interest_score >= go_topics[0].interest_score

    def test_lookback_window(self, tmp_path, monkeypatch):
        """Entries older than lookback_days are excluded."""
        _write_feedback({}, monkeypatch)
        entries = [
            {"query": "old topic question", "coverage": 0.2, "timestamp": _ts(60)},
            {"query": "old topic again", "coverage": 0.2, "timestamp": _ts(60)},
            {"query": "recent topic here", "coverage": 0.2, "timestamp": _ts(1)},
            {"query": "recent topic again", "coverage": 0.2, "timestamp": _ts(2)},
        ]
        _write_query_log(str(tmp_path), entries)

        result = extract_interests(data_path=str(tmp_path), lookback_days=30, min_queries=2)
        topics = [t.topic for t in result]
        assert any("recent" in t for t in topics)
        # Old entries should be filtered out
        assert not any("old" in t for t in topics)

    def test_max_topics_limit(self, tmp_path, monkeypatch):
        """Respects max_topics limit."""
        _write_feedback({}, monkeypatch)
        entries = []
        for i in range(10):
            for j in range(3):
                entries.append(
                    {
                        "query": f"topic{i} question{j}",
                        "coverage": 0.2,
                        "timestamp": _ts(1),
                    }
                )
        _write_query_log(str(tmp_path), entries)

        result = extract_interests(data_path=str(tmp_path), min_queries=2, max_topics=5)
        assert len(result) <= 5

    def test_sample_queries_deduped(self, tmp_path, monkeypatch):
        """sample_queries should be deduplicated."""
        _write_feedback({}, monkeypatch)
        entries = [
            {"query": "same query", "coverage": 0.2, "timestamp": _ts(1)},
            {"query": "same query", "coverage": 0.3, "timestamp": _ts(2)},
            {"query": "different query", "coverage": 0.25, "timestamp": _ts(3)},
        ]
        _write_query_log(str(tmp_path), entries)

        result = extract_interests(data_path=str(tmp_path), min_queries=2)
        if result:
            for topic in result:
                assert len(topic.sample_queries) == len(set(topic.sample_queries))


# ── generate_proactive_queries ───────────────────────────────────────────────


class TestGenerateProactiveQueries:
    def _make_interests(self) -> list[InterestTopic]:
        return [
            InterestTopic(
                topic="docker compose",
                query_count=5,
                avg_coverage=0.3,
                adopt_score=3,
                interest_score=0.8,
            ),
            InterestTopic(
                topic="kubernetes 部署",
                query_count=3,
                avg_coverage=0.4,
                adopt_score=1,
                interest_score=0.6,
            ),
            InterestTopic(
                topic="well covered",
                query_count=10,
                avg_coverage=0.9,
                adopt_score=5,
                interest_score=0.5,
            ),
        ]

    def test_rule_based_generation(self):
        """Rule-based generates queries with appropriate suffixes."""
        interests = self._make_interests()
        queries = generate_proactive_queries(interests, max_queries=5)
        assert len(queries) > 0
        # Docker topic should have English suffix
        docker_qs = [q for q in queries if q.topic == "docker compose"]
        assert docker_qs
        assert any(kw in docker_qs[0].query for kw in ("latest", "2026", "common issues"))

    def test_chinese_topic_gets_chinese_suffix(self):
        """CJK topics get Chinese suffixes."""
        interests = self._make_interests()
        queries = generate_proactive_queries(interests, max_queries=5)
        k8s_qs = [q for q in queries if q.topic == "kubernetes 部署"]
        assert k8s_qs
        assert any(kw in k8s_qs[0].query for kw in ("最新进展", "最佳实践", "常见问题"))

    def test_skips_high_coverage(self):
        """Topics with avg_coverage > 0.7 are skipped."""
        interests = self._make_interests()
        queries = generate_proactive_queries(interests, max_queries=10)
        topics = {q.topic for q in queries}
        assert "well covered" not in topics

    def test_dedup_against_existing(self):
        """Existing queries are not regenerated."""
        interests = [
            InterestTopic(
                topic="docker compose",
                query_count=5,
                avg_coverage=0.3,
                adopt_score=3,
                interest_score=0.8,
            ),
        ]
        existing = {"docker compose latest best practices"}
        queries = generate_proactive_queries(interests, existing_queries=existing, max_queries=5)
        for q in queries:
            assert q.query.lower() not in {e.lower() for e in existing}

    def test_max_queries_respected(self):
        """Output respects max_queries limit."""
        interests = [
            InterestTopic(
                topic=f"topic{i}",
                query_count=3,
                avg_coverage=0.2,
                adopt_score=1,
                interest_score=0.8 - i * 0.1,
            )
            for i in range(10)
        ]
        queries = generate_proactive_queries(interests, max_queries=3)
        assert len(queries) <= 3

    def test_empty_interests(self):
        """No interests → no queries."""
        queries = generate_proactive_queries([], max_queries=5)
        assert queries == []

    def test_llm_fallback_on_missing_config(self, monkeypatch):
        """use_llm=True falls back to rules when LLM is not configured."""
        monkeypatch.setattr("curator.config.OAI_BASE", "")
        monkeypatch.setattr("curator.config.OAI_KEY", "")
        monkeypatch.setattr("curator.config.ROUTER_MODELS", [])
        interests = [
            InterestTopic(
                topic="test topic",
                query_count=3,
                avg_coverage=0.3,
                adopt_score=1,
                interest_score=0.7,
            ),
        ]
        queries = generate_proactive_queries(interests, use_llm=True, max_queries=3)
        # Should still return results (from rule-based fallback)
        assert len(queries) > 0
        assert queries[0].reason == "high_interest_low_coverage"

    def test_llm_path_success(self, monkeypatch):
        """LLM path parses JSON array from chat response."""

        def mock_chat(*args, **kwargs):
            return '[{"query": "advanced docker networking 2026", "topic": "docker compose"}]'

        monkeypatch.setattr("curator.config.OAI_BASE", "http://fake")
        monkeypatch.setattr("curator.config.OAI_KEY", "fake-key")
        monkeypatch.setattr("curator.config.ROUTER_MODELS", ["test-model"])
        monkeypatch.setattr("curator.config.chat", mock_chat)

        interests = [
            InterestTopic(
                topic="docker compose",
                query_count=5,
                avg_coverage=0.3,
                adopt_score=3,
                interest_score=0.8,
            ),
        ]
        queries = generate_proactive_queries(interests, use_llm=True, max_queries=3)
        assert len(queries) == 1
        assert queries[0].query == "advanced docker networking 2026"
        assert queries[0].reason == "llm_generated"

    def test_llm_path_bad_response_falls_back(self, monkeypatch):
        """LLM returning unparseable text falls back to rules."""

        def mock_chat(*args, **kwargs):
            return "Sorry, I cannot help with that."

        monkeypatch.setattr("curator.config.OAI_BASE", "http://fake")
        monkeypatch.setattr("curator.config.OAI_KEY", "fake-key")
        monkeypatch.setattr("curator.config.ROUTER_MODELS", ["test-model"])
        monkeypatch.setattr("curator.config.chat", mock_chat)

        interests = [
            InterestTopic(
                topic="docker compose",
                query_count=5,
                avg_coverage=0.3,
                adopt_score=3,
                interest_score=0.8,
            ),
        ]
        queries = generate_proactive_queries(interests, use_llm=True, max_queries=3)
        assert len(queries) > 0
        assert queries[0].reason == "high_interest_low_coverage"  # fell back to rules

    def test_llm_path_exception_falls_back(self, monkeypatch):
        """LLM chat exception falls back to rules."""

        def mock_chat(*args, **kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("curator.config.OAI_BASE", "http://fake")
        monkeypatch.setattr("curator.config.OAI_KEY", "fake-key")
        monkeypatch.setattr("curator.config.ROUTER_MODELS", ["test-model"])
        monkeypatch.setattr("curator.config.chat", mock_chat)

        interests = [
            InterestTopic(
                topic="docker compose",
                query_count=5,
                avg_coverage=0.3,
                adopt_score=3,
                interest_score=0.8,
            ),
        ]
        queries = generate_proactive_queries(interests, use_llm=True, max_queries=3)
        assert len(queries) > 0
