"""Tests for curator.governance_report."""

from __future__ import annotations

import json

import pytest

from curator.governance_report import (
    format_report,
    format_report_html,
    format_report_json,
)


def _sample_report(mode: str = "normal") -> dict:
    """Create a sample governance report for testing."""
    report = {
        "cycle_id": "gov_cycle_20260227_100000",
        "timestamp": "2026-02-27T10:00:00+00:00",
        "mode": mode,
        "overview": {
            "total_resources": 42,
            "health_score": 75,
        },
        "knowledge_health": {
            "fresh": 30,
            "aging": 8,
            "stale": 4,
            "coverage_mean": 0.623,
        },
        "flags": {
            "total": 6,
            "by_type": {
                "stale_resource": 3,
                "broken_url": 2,
                "review_expired": 1,
            },
        },
        "proactive": {
            "queries_run": 3,
            "ingested": 2,
            "async_queued": 5,
            "dry_run": False,
        },
        "async_harvest": {
            "harvested": 4,
            "ingested": 3,
        },
        "pending_review_count": 2,
        "weak_topics": [
            {"topic": "redis caching", "avg_coverage": 0.32},
            {"topic": "kubernetes deploy", "avg_coverage": 0.41},
        ],
        "duration_sec": 12.5,
    }

    if mode == "team":
        report["query_metrics"] = {
            "total_queries": 100,
            "coverage": {"mean": 0.62, "p50": 0.65},
        }
        report["ttl_suggestions"] = [
            {"file": "test.md", "current_tier": "cold", "suggested_tier": "hot"},
        ]
        report["audit_log"] = [
            {"phase": "collect", "action": "analyze_weak", "outcome": "found_5"},
            {"phase": "audit", "action": "freshness_scan", "outcome": "scanned_42"},
            {"phase": "flag", "action": "create_flags", "outcome": "created_6"},
            {"phase": "report", "action": "generate_report", "outcome": "ok"},
        ]
        report["config_snapshot"] = {
            "governance_enabled": "1",
            "governance_mode": "team",
            "cov_sufficient": "0.55",
        }

    return report


class TestFormatReportASCII:
    def test_basic_output(self):
        report = _sample_report()
        text = format_report(report)
        assert "Governance Report" in text
        assert "gov_cycle_20260227_100000" in text
        assert "42" in text  # total resources
        assert "75/100" in text  # health score

    def test_contains_sections(self):
        text = format_report(_sample_report())
        assert "Knowledge Health" in text
        assert "Flags" in text
        assert "Proactive Search" in text

    def test_dry_run_shown(self):
        report = _sample_report()
        report["proactive"]["dry_run"] = True
        text = format_report(report)
        assert "SKIPPED" in text or "dry run" in text

    def test_weak_topics_listed(self):
        text = format_report(_sample_report())
        assert "redis caching" in text
        assert "0.32" in text

    def test_unknown_health_score(self):
        report = _sample_report()
        report["overview"]["health_score"] = -1
        text = format_report(report)
        assert "unknown" in text

    def test_empty_report(self):
        report = {
            "cycle_id": "empty",
            "timestamp": "2026-01-01T00:00:00Z",
            "mode": "normal",
            "overview": {"total_resources": 0, "health_score": -1},
            "knowledge_health": {},
            "flags": {"total": 0, "by_type": {}},
            "proactive": {"dry_run": True},
        }
        text = format_report(report)
        assert "Governance Report" in text
        assert "empty" in text

    def test_cjk_topic_display(self):
        """CJK characters in topics should not break box alignment."""
        report = _sample_report()
        report["weak_topics"] = [{"topic": "容器编排部署", "avg_coverage": 0.3}]
        text = format_report(report)
        assert "容器编排部署" in text
        # Box should still be well-formed
        lines = text.split("\n")
        assert lines[0].startswith("┌")
        assert lines[-1].startswith("└")


class TestFormatReportJSON:
    def test_valid_json(self):
        text = format_report_json(_sample_report())
        data = json.loads(text)
        assert data["cycle_id"] == "gov_cycle_20260227_100000"
        assert data["overview"]["health_score"] == 75

    def test_team_mode_includes_extras(self):
        text = format_report_json(_sample_report("team"))
        data = json.loads(text)
        assert "query_metrics" in data
        assert "config_snapshot" in data
        assert "audit_log" in data


class TestFormatReportHTML:
    def test_contains_html_table(self):
        html = format_report_html(_sample_report())
        assert "<table" in html
        assert "curator-governance-report" in html
        assert "Governance Report" in html

    def test_html_escaping(self):
        """Special chars in values should be escaped."""
        report = _sample_report()
        report["cycle_id"] = '<script>alert("xss")</script>'
        html = format_report_html(report)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_team_mode_has_config_and_audit(self):
        html = format_report_html(_sample_report("team"))
        assert "Config Snapshot" in html
        assert "Audit Log" in html
        assert "governance_enabled" in html

    def test_normal_mode_no_team_extras(self):
        html = format_report_html(_sample_report("normal"))
        assert "Config Snapshot" not in html
        assert "Audit Log" not in html

    def test_dry_run_html(self):
        report = _sample_report()
        report["proactive"]["dry_run"] = True
        html = format_report_html(report)
        assert "SKIPPED" in html


class TestAsyncSections:
    def test_ascii_shows_async_queued(self):
        text = format_report(_sample_report())
        assert "Async Queued" in text
        assert "5" in text

    def test_ascii_shows_harvest(self):
        text = format_report(_sample_report())
        assert "Async Harvest" in text
        assert "Harvested" in text

    def test_ascii_no_harvest_when_zero(self):
        report = _sample_report()
        report["async_harvest"] = {"harvested": 0, "ingested": 0}
        text = format_report(report)
        assert "Async Harvest" not in text

    def test_html_shows_async_queued(self):
        html = format_report_html(_sample_report())
        assert "Async Queued" in html

    def test_html_shows_harvest(self):
        html = format_report_html(_sample_report())
        assert "Async Harvest" in html

    def test_json_includes_async_fields(self):
        text = format_report_json(_sample_report())
        data = json.loads(text)
        assert data["proactive"]["async_queued"] == 5
        assert data["async_harvest"]["harvested"] == 4
        assert data["async_harvest"]["ingested"] == 3
