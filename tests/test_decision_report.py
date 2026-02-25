"""Tests for curator/decision_report.py."""
import unittest


def _make_result(**kwargs) -> dict:
    """Build a minimal pipeline result dict for testing."""
    base = {
        "query": "test query",
        "ov_results": {},
        "context_text": "",
        "external_text": "",
        "coverage": 0.0,
        "conflict": {"has_conflict": False, "summary": "", "points": []},
        "meta": {
            "coverage": 0.65,
            "coverage_reason": "local_sufficient",
            "external_triggered": False,
            "external_reason": "local_sufficient",
            "has_conflict": False,
            "ingested": False,
            "used_uris": ["viking://a", "viking://b"],
            "warnings": [],
            "memories_count": 1,
            "resources_count": 2,
            "skills_count": 0,
            "decision_trace": {
                "load_stage": "L0",
                "llm_calls": 0,
                "external_reason": "local_sufficient",
            },
        },
        "metrics": {"duration_sec": 1.23, "flags": {}, "scores": {}},
        "case_path": None,
        "decision_report": "",
    }
    # Deep merge kwargs into base
    for k, v in kwargs.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


class TestFormatReport(unittest.TestCase):

    def test_returns_string(self):
        from curator.decision_report import format_report
        report = format_report(_make_result())
        self.assertIsInstance(report, str)

    def test_contains_key_fields(self):
        from curator.decision_report import format_report
        result = _make_result(query="docker healthcheck 配置")
        report = format_report(result)
        self.assertIn("docker", report)
        self.assertIn("0.65", report)          # coverage
        self.assertIn("local_sufficient", report)
        self.assertIn("L0", report)
        self.assertIn("2", report)             # used URIs count
        self.assertIn("No", report)            # external = No
        self.assertIn("0", report)             # llm_calls = 0

    def test_long_query_truncated(self):
        from curator.decision_report import format_report
        long_query = "x" * 100
        report = format_report(_make_result(query=long_query))
        # Should not crash and should contain truncation marker
        self.assertIn("…", report)

    def test_empty_result_no_crash(self):
        """Empty dict should not raise an exception."""
        from curator.decision_report import format_report
        report = format_report({})
        self.assertIsInstance(report, str)
        self.assertIn("Curator", report)

    def test_conflict_shown_when_present(self):
        from curator.decision_report import format_report
        result = _make_result()
        result["conflict"] = {
            "has_conflict": True,
            "summary": "内容时效冲突",
            "points": [],
        }
        report = format_report(result)
        self.assertIn("内容时效冲突", report)

    def test_conflict_none_when_absent(self):
        from curator.decision_report import format_report
        report = format_report(_make_result())
        self.assertIn("None", report)  # conflict = None

    def test_external_yes_when_triggered(self):
        from curator.decision_report import format_report
        result = _make_result()
        result["meta"]["external_triggered"] = True
        report = format_report(result)
        self.assertIn("Yes", report)

    def test_border_characters_present(self):
        """Box drawing characters should appear."""
        from curator.decision_report import format_report
        report = format_report(_make_result())
        self.assertIn("┌", report)
        self.assertIn("└", report)
        self.assertIn("│", report)

    def test_ingested_yes(self):
        from curator.decision_report import format_report
        result = _make_result()
        result["meta"]["ingested"] = True
        report = format_report(result)
        self.assertIn("Yes", report)

    def test_warnings_shown_when_present(self):
        from curator.decision_report import format_report
        result = _make_result()
        result["meta"]["warnings"] = ["stale_source", "low_trust"]
        report = format_report(result)
        self.assertIn("stale_source", report)


class TestFormatReportShort(unittest.TestCase):

    def test_single_line(self):
        from curator.decision_report import format_report_short
        report = format_report_short(_make_result())
        lines = [l for l in report.split("\n") if l.strip()]
        self.assertEqual(len(lines), 1)

    def test_contains_curator_prefix(self):
        from curator.decision_report import format_report_short
        report = format_report_short(_make_result())
        self.assertTrue(report.startswith("[Curator]"))

    def test_contains_key_metrics(self):
        from curator.decision_report import format_report_short
        report = format_report_short(_make_result())
        self.assertIn("cov=0.65", report)
        self.assertIn("stage=L0", report)
        self.assertIn("used=2", report)
        self.assertIn("ext=No", report)
        self.assertIn("llm=0", report)
        self.assertIn("conflict=No", report)

    def test_empty_result_no_crash(self):
        from curator.decision_report import format_report_short
        report = format_report_short({})
        self.assertTrue(report.startswith("[Curator]"))
