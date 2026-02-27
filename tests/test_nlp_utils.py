"""Tests for curator.nlp_utils — keyword extraction, topic extraction, weak topic analysis."""

import json
import os

import pytest

from curator.nlp_utils import (
    analyze_weak_topics,
    extract_keywords,
    extract_topic,
    extract_topic_coarse,
)

# ── extract_keywords ──


class TestExtractKeywords:
    def test_english(self):
        assert extract_keywords("how to deploy Redis") == ["deploy", "redis"]

    def test_chinese(self):
        kws = extract_keywords("如何部署 Redis")
        assert "redis" in kws

    def test_stopwords_removed(self):
        assert extract_keywords("the a an is") == []

    def test_empty(self):
        assert extract_keywords("") == []

    def test_short_tokens_removed(self):
        # single-char tokens dropped
        assert "a" not in extract_keywords("a b c docker")


# ── extract_topic ──


class TestExtractTopic:
    def test_top_3_keywords(self):
        topic = extract_topic("nginx reverse proxy ssl configuration")
        parts = topic.split()
        assert len(parts) <= 3

    def test_empty_falls_back(self):
        # no keywords → returns truncated raw query
        result = extract_topic("the a an")
        assert isinstance(result, str)

    def test_coarse_top_2(self):
        coarse = extract_topic_coarse("docker compose networking setup guide")
        assert len(coarse.split()) <= 2


# ── analyze_weak_topics ──


class TestAnalyzeWeakTopics:
    def _write_log(self, path: str, entries: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_no_log_file_returns_empty(self, tmp_path):
        result = analyze_weak_topics(str(tmp_path))
        assert result == []

    def test_empty_log_returns_empty(self, tmp_path):
        self._write_log(str(tmp_path / "query_log.jsonl"), [])
        assert analyze_weak_topics(str(tmp_path)) == []

    def test_detects_weak_topic(self, tmp_path):
        entries = [
            {"query": "redis deployment", "coverage": 0.1, "external_triggered": True},
            {"query": "redis deployment", "coverage": 0.15, "external_triggered": True},
            {"query": "redis deployment", "coverage": 0.2, "external_triggered": True},
        ]
        self._write_log(str(tmp_path / "query_log.jsonl"), entries)
        result = analyze_weak_topics(str(tmp_path), min_queries=2)
        assert len(result) == 1
        assert result[0]["external_rate"] == 1.0

    def test_strong_topic_excluded(self, tmp_path):
        # external_rate = 0.0 → not weak
        entries = [
            {"query": "docker setup", "coverage": 0.8, "external_triggered": False},
            {"query": "docker setup", "coverage": 0.9, "external_triggered": False},
        ]
        self._write_log(str(tmp_path / "query_log.jsonl"), entries)
        assert analyze_weak_topics(str(tmp_path), min_queries=2) == []

    def test_min_queries_filter(self, tmp_path):
        # Only 1 query → below min_queries=2
        entries = [
            {"query": "obscure topic", "coverage": 0.05, "external_triggered": True},
        ]
        self._write_log(str(tmp_path / "query_log.jsonl"), entries)
        assert analyze_weak_topics(str(tmp_path), min_queries=2) == []

    def test_sorted_by_external_rate(self, tmp_path):
        entries = [
            # topic A: external_rate=0.5
            {"query": "nginx proxy", "coverage": 0.3, "external_triggered": True},
            {"query": "nginx proxy", "coverage": 0.3, "external_triggered": False},
            # topic B: external_rate=1.0
            {"query": "kubernetes secrets", "coverage": 0.1, "external_triggered": True},
            {"query": "kubernetes secrets", "coverage": 0.1, "external_triggered": True},
        ]
        self._write_log(str(tmp_path / "query_log.jsonl"), entries)
        result = analyze_weak_topics(str(tmp_path), min_queries=2)
        # external_rate=1.0 first (kubernetes), then 0.5 (nginx) if above threshold
        # nginx external_rate=0.5 is NOT > 0.5, so only kubernetes
        assert len(result) == 1
        assert "kubernetes" in result[0]["topic"]

    def test_result_fields(self, tmp_path):
        entries = [
            {"query": "redis cluster", "coverage": 0.2, "external_triggered": True},
            {"query": "redis cluster", "coverage": 0.3, "external_triggered": True},
        ]
        self._write_log(str(tmp_path / "query_log.jsonl"), entries)
        result = analyze_weak_topics(str(tmp_path), min_queries=2)
        assert len(result) == 1
        r = result[0]
        assert "topic" in r
        assert "query_count" in r
        assert "avg_coverage" in r
        assert "external_rate" in r
        assert r["query_count"] == 2

    def test_malformed_lines_skipped(self, tmp_path):
        log_path = str(tmp_path / "query_log.jsonl")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps({"query": "redis config", "coverage": 0.1, "external_triggered": True}) + "\n")
            f.write(json.dumps({"query": "redis config", "coverage": 0.1, "external_triggered": True}) + "\n")
        result = analyze_weak_topics(str(tmp_path), min_queries=2)
        assert len(result) == 1
