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
        self.assertIn("0.65", report)  # coverage
        self.assertIn("local_sufficient", report)
        self.assertIn("L0", report)
        self.assertIn("Used URIs   : 2", report)  # 精确匹配字段，避免 "2" 假阳性
        self.assertIn("External    : No", report)
        self.assertIn("LLM calls   : 0", report)

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
        lines = [line for line in report.split("\n") if line.strip()]
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

    def test_conflict_true_no_summary_fallback(self):
        """conflict has_conflict=True but summary='' → 'Yes (no summary)'."""
        from curator.decision_report import format_report

        result = _make_result()
        result["conflict"] = {"has_conflict": True, "summary": "", "points": []}
        report = format_report(result)
        self.assertIn("Yes (no summary)", report)

    def test_cjk_rows_do_not_crash(self):
        """CJK content in query/conflict should not raise exceptions."""
        from curator.decision_report import format_report

        result = _make_result(query="如何在 Docker 中配置健康检查？" * 3)
        result["conflict"] = {"has_conflict": True, "summary": "本地知识库内容与外部搜索结果存在时效冲突", "points": []}
        report = format_report(result)  # should not raise
        self.assertIsInstance(report, str)
        self.assertIn("│", report)

    def test_display_width_cjk(self):
        """_display_width should count CJK chars as 2 columns."""
        from curator.decision_report import _display_width

        self.assertEqual(_display_width("ABC"), 3)
        self.assertEqual(_display_width("中文"), 4)
        self.assertEqual(_display_width("A中B"), 4)


class TestTruncateTo(unittest.TestCase):
    """Tests for _truncate_to() — exact-fit edge cases included."""

    def setUp(self):
        from curator.decision_report import _truncate_to

        self.t = _truncate_to

    def test_short_string_unchanged(self):
        self.assertEqual(self.t("AB", 4), "AB")

    def test_exact_fit_no_truncation(self):
        """ABCD with max_width=4 should return 'ABCD', not 'ABC…'."""
        self.assertEqual(self.t("ABCD", 4), "ABCD")

    def test_one_over_truncated(self):
        """ABCDE with max_width=4 should return 'ABC…'."""
        self.assertEqual(self.t("ABCDE", 4), "ABC…")

    def test_cjk_exact_fit(self):
        """A中 display-width=3, max_width=3 → no truncation."""
        self.assertEqual(self.t("A中", 3), "A中")

    def test_cjk_one_over(self):
        """A中B display-width=4, max_width=3 → 'A…'."""
        self.assertEqual(self.t("A中B", 3), "A…")

    def test_cjk_overflow_char(self):
        """AB中 where 中 doesn't fit: display is 4, max_width=3 → 'AB…'."""
        self.assertEqual(self.t("AB中", 3), "AB…")

    def test_empty_string(self):
        self.assertEqual(self.t("", 4), "")

    def test_single_char_fits(self):
        self.assertEqual(self.t("A", 1), "A")

    def test_single_cjk_fits(self):
        self.assertEqual(self.t("中", 2), "中")

    def test_single_cjk_no_room(self):
        """CJK char (width 2) with max_width=1: can't fit, return '…'."""
        self.assertEqual(self.t("中", 1), "…")


class TestExtractReportFields(unittest.TestCase):
    def test_all_keys_present(self):
        from curator.decision_report import _extract_report_fields

        f = _extract_report_fields(_make_result())
        for key in (
            "query",
            "run_id",
            "coverage",
            "coverage_reason",
            "load_stage",
            "used_uris",
            "external_triggered",
            "cache_hit",
            "llm_calls",
            "has_conflict",
            "conflict_summary",
            "ingested",
            "duration_sec",
            "warnings",
        ):
            self.assertIn(key, f)

    def test_empty_result_no_crash(self):
        from curator.decision_report import _extract_report_fields

        f = _extract_report_fields({})
        self.assertEqual(f["coverage"], 0.0)
        self.assertEqual(f["llm_calls"], 0)
        self.assertFalse(f["has_conflict"])

    def test_run_id_extracted(self):
        from curator.decision_report import _extract_report_fields

        f = _extract_report_fields({**_make_result(), "run_id": "abc12345"})
        self.assertEqual(f["run_id"], "abc12345")


class TestFormatReportJson(unittest.TestCase):
    def test_returns_valid_json(self):
        import json

        from curator.decision_report import format_report_json

        s = format_report_json(_make_result())
        data = json.loads(s)
        self.assertIsInstance(data, dict)

    def test_contains_key_fields(self):
        import json

        from curator.decision_report import format_report_json

        data = json.loads(format_report_json(_make_result()))
        self.assertIn("coverage", data)
        self.assertIn("coverage_reason", data)
        self.assertIn("load_stage", data)
        self.assertIn("used_uris_count", data)
        self.assertIn("llm_calls", data)

    def test_coverage_value(self):
        import json

        from curator.decision_report import format_report_json

        data = json.loads(format_report_json(_make_result()))
        self.assertAlmostEqual(data["coverage"], 0.65, places=2)

    def test_used_uris_count(self):
        import json

        from curator.decision_report import format_report_json

        data = json.loads(format_report_json(_make_result()))
        self.assertEqual(data["used_uris_count"], 2)

    def test_empty_result_no_crash(self):
        import json

        from curator.decision_report import format_report_json

        data = json.loads(format_report_json({}))
        self.assertFalse(data["has_conflict"])
        self.assertEqual(data["llm_calls"], 0)

    def test_ensure_ascii_false(self):
        from curator.decision_report import format_report_json

        result = _make_result(query="中文查询")
        s = format_report_json(result)
        self.assertIn("中文查询", s)

    def test_conflict_fields(self):
        import json

        from curator.decision_report import format_report_json

        result = _make_result(conflict={"has_conflict": True, "summary": "版本冲突", "points": []})
        data = json.loads(format_report_json(result))
        self.assertTrue(data["has_conflict"])
        self.assertEqual(data["conflict_summary"], "版本冲突")


class TestFormatReportHtml(unittest.TestCase):
    def test_returns_string(self):
        from curator.decision_report import format_report_html

        s = format_report_html(_make_result())
        self.assertIsInstance(s, str)

    def test_contains_div_and_table(self):
        from curator.decision_report import format_report_html

        s = format_report_html(_make_result())
        self.assertIn("<div", s)
        self.assertIn("<table", s)
        self.assertIn("</table>", s)
        self.assertIn("</div>", s)

    def test_contains_key_labels(self):
        from curator.decision_report import format_report_html

        s = format_report_html(_make_result())
        self.assertIn("Coverage", s)
        self.assertIn("LLM Calls", s)
        self.assertIn("External", s)

    def test_html_escaping(self):
        from curator.decision_report import format_report_html

        result = _make_result(query='<script>alert("xss")</script>')
        s = format_report_html(result)
        self.assertNotIn("<script>", s)
        self.assertIn("&lt;script&gt;", s)

    def test_empty_result_no_crash(self):
        from curator.decision_report import format_report_html

        s = format_report_html({})
        self.assertIn("<table", s)

    def test_warnings_shown(self):
        from curator.decision_report import format_report_html

        result = _make_result()
        result["meta"]["warnings"] = ["stale source", "low trust"]
        s = format_report_html(result)
        self.assertIn("Warnings", s)
        self.assertIn("stale source", s)
