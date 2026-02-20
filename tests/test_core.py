#!/usr/bin/env python3
"""Unit tests for OpenViking Curator v2 core functions."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing curator
os.environ.setdefault('OAI_BASE', 'http://localhost:8000/v1')
os.environ.setdefault('OAI_KEY', 'test-key')
os.environ.setdefault('GROK_SEARCH_URL', 'http://localhost:8788')

import feedback_store
from curator import (
    route_scope,
    validate_config,
    # v2 modules
    ov_retrieve,
    load_context,
    assess_coverage,
)
from curator_query import should_route


# ─── route_scope ─────────────────────────────────────────────

class TestRouteScope(unittest.TestCase):
    """Test query scope routing (lightweight, rule-based only)."""

    def test_domain_detection(self):
        scope = route_scope("Python async 怎么用")
        self.assertEqual(scope['domain'], 'technology')

    def test_general_domain_fallback(self):
        scope = route_scope("今天天气怎么样")
        self.assertEqual(scope['domain'], 'general')
        self.assertIsInstance(scope['keywords'], list)

    def test_keywords_extraction(self):
        scope = route_scope("Nginx 反向代理 502 怎么排查？")
        kw_lower = [k.lower() for k in scope['keywords']]
        self.assertIn('nginx', kw_lower)

    def test_need_fresh(self):
        scope = route_scope("2026 最新 Python 发布了什么")
        self.assertTrue(scope['need_fresh'])

    def test_no_source_pref(self):
        """v2 router no longer returns source_pref/exclude/confidence."""
        scope = route_scope("Docker 部署指南")
        self.assertNotIn('source_pref', scope)
        self.assertNotIn('confidence', scope)
        self.assertNotIn('exclude', scope)


# ─── assess_coverage (v2) ────────────────────────────────────

class TestAssessCoverage(unittest.TestCase):
    """Test simplified coverage assessment."""

    def test_no_results(self):
        cov, need_ext, reason = assess_coverage({"all_items": []})
        self.assertEqual(cov, 0.0)
        self.assertTrue(need_ext)
        self.assertEqual(reason, "no_results")

    def test_no_scores(self):
        cov, need_ext, reason = assess_coverage({"all_items": [{"uri": "x", "score": 0}]})
        self.assertEqual(cov, 0.0)
        self.assertTrue(need_ext)
        self.assertEqual(reason, "no_scores")

    def test_high_score_sufficient(self):
        items = [
            {"uri": "a", "score": 0.7},
            {"uri": "b", "score": 0.6},
            {"uri": "c", "score": 0.5},
        ]
        cov, need_ext, reason = assess_coverage({"all_items": items})
        self.assertGreater(cov, 0.5)
        self.assertFalse(need_ext)
        self.assertEqual(reason, "local_sufficient")

    def test_marginal_coverage(self):
        items = [{"uri": "a", "score": 0.5}]
        cov, need_ext, reason = assess_coverage({"all_items": items})
        self.assertFalse(need_ext)
        self.assertEqual(reason, "local_marginal")

    def test_low_score_triggers_external(self):
        items = [{"uri": "a", "score": 0.3}]
        cov, need_ext, reason = assess_coverage({"all_items": items})
        self.assertTrue(need_ext)
        self.assertIn(reason, ("low_coverage", "insufficient"))


# ─── load_context (v2, strict on-demand) ─────────────────────

class TestLoadContext(unittest.TestCase):
    """Test strict L0→L1→L2 on-demand loading."""

    def test_empty_items(self):
        mock_ov = MagicMock()
        text, uris, stage = load_context(mock_ov, [], "test query")
        self.assertEqual(text, "")
        self.assertEqual(uris, [])

    def test_l0_sufficient_skips_l1_l2(self):
        """When L0 abstracts are rich and score is high, skip L1/L2."""
        mock_ov = MagicMock()
        items = [
            {"uri": "a", "score": 0.8, "abstract": "A detailed abstract about Docker deployment with Nginx reverse proxy and SSL configuration. " * 3},
            {"uri": "b", "score": 0.7, "abstract": "Another detailed abstract about container orchestration and systemd service management. " * 3},
        ]
        text, uris, stage = load_context(mock_ov, items, "test query")
        # L0 is enough: overview and read should NOT be called
        mock_ov.overview.assert_not_called()
        mock_ov.read.assert_not_called()
        self.assertEqual(len(uris), 2)

    def test_l1_used_when_l0_insufficient(self):
        """When L0 is thin, L1 (overview) should be fetched."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "This is a detailed overview of the topic with enough content."

        items = [{"uri": "a", "score": 0.4, "abstract": "short"}]
        text, uris, stage = load_context(mock_ov, items, "test query")

        mock_ov.overview.assert_called()
        mock_ov.read.assert_not_called()

    def test_l2_only_when_l1_insufficient(self):
        """L2 should only trigger when L1 is also not enough."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "Overview content here with enough text."
        mock_ov.read.return_value = "Full detailed content of the document for deep reading."

        # Single item, score below L1-enough threshold
        items = [{"uri": "a", "score": 0.45, "abstract": "short"}]
        text, uris, stage = load_context(mock_ov, items, "test query")

        # L1 not enough (only 1 source, score < 0.5), so L2 should be attempted
        self.assertIn("a", uris)

    def test_max_l2_respected(self):
        """Should not read more than max_l2 items at L2."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "Overview text sufficient for display."
        mock_ov.read.return_value = "Full content for reading."

        items = [
            {"uri": f"item_{i}", "score": 0.8 - i * 0.01, "abstract": "abs"}
            for i in range(5)
        ]
        text, uris, stage = load_context(mock_ov, items, "test", max_l2=2)

        self.assertLessEqual(mock_ov.read.call_count, 2)

    def test_max_l2_zero_skips_read(self):
        """max_l2=0 should never call read()."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "Overview with reasonable content."

        items = [{"uri": "a", "score": 0.9, "abstract": "short"}]
        text, uris, stage = load_context(mock_ov, items, "test", max_l2=0)

        mock_ov.read.assert_not_called()


# ─── feedback_store (with file lock) ─────────────────────────

class TestFeedbackStore(unittest.TestCase):

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(
            suffix='.json', delete=False
        )
        self.tmpfile.close()
        feedback_store.STORE = Path(self.tmpfile.name)
        Path(self.tmpfile.name).write_text('{}', encoding='utf-8')

    def tearDown(self):
        os.unlink(self.tmpfile.name)

    def test_apply_increments(self):
        result = feedback_store.apply('viking://test', 'up')
        self.assertEqual(result['up'], 1)
        result = feedback_store.apply('viking://test', 'up')
        self.assertEqual(result['up'], 2)

    def test_load_after_apply(self):
        feedback_store.apply('viking://x', 'adopt')
        data = feedback_store.load()
        self.assertEqual(data['viking://x']['adopt'], 1)

    def test_invalid_action(self):
        with self.assertRaises(ValueError):
            feedback_store.apply('viking://x', 'invalid')

    def test_concurrent_safety(self):
        for i in range(20):
            feedback_store.apply('viking://concurrent', 'up')
        data = feedback_store.load()
        self.assertEqual(data['viking://concurrent']['up'], 20)


# ─── validate_config ─────────────────────────────────────────

class TestValidateConfig(unittest.TestCase):

    def test_valid_config(self):
        import curator.config as cfg
        old = (cfg.OAI_BASE, cfg.OAI_KEY, cfg.GROK_KEY)
        cfg.OAI_BASE, cfg.OAI_KEY, cfg.GROK_KEY = 'http://x', 'k', 'g'
        try:
            cfg.validate_config()
        finally:
            cfg.OAI_BASE, cfg.OAI_KEY, cfg.GROK_KEY = old

    @patch.dict(os.environ, {'CURATOR_OAI_BASE': '', 'CURATOR_OAI_KEY': ''}, clear=False)
    def test_missing_oai_raises(self):
        import curator.config as cfg
        old_base, old_key = cfg.OAI_BASE, cfg.OAI_KEY
        cfg.OAI_BASE, cfg.OAI_KEY = '', ''
        try:
            with self.assertRaises(RuntimeError) as ctx:
                cfg.validate_config()
            self.assertIn('CURATOR_OAI_BASE', str(ctx.exception))
        finally:
            cfg.OAI_BASE, cfg.OAI_KEY = old_base, old_key


# ─── should_route (curator_query gate) ───────────────────────

class TestShouldRoute(unittest.TestCase):

    def test_tech_question_routes(self):
        routed, reason = should_route('Redis 和 Memcached 怎么选')
        self.assertTrue(routed)

    def test_greeting_blocks(self):
        routed, reason = should_route('你好')
        self.assertFalse(routed)

    def test_positive_overrides_weak_negative(self):
        routed, reason = should_route('ok 那 Docker 怎么部署')
        self.assertTrue(routed)

    def test_strong_negative_blocks(self):
        routed, reason = should_route('今天天气怎么样')
        self.assertFalse(routed)

    def test_empty_query(self):
        routed, reason = should_route('')
        self.assertFalse(routed)

    def test_pure_command_blocks(self):
        routed, reason = should_route('帮我跑一下 git status')
        self.assertFalse(routed)


# ─── OVClient.wait_processed ─────────────────────────────────

class TestOVClientWaitProcessed(unittest.TestCase):

    def test_wait_processed_method_exists(self):
        """OVClient should have wait_processed method."""
        from curator.session_manager import OVClient
        self.assertTrue(hasattr(OVClient, 'wait_processed'))


# ─── uri_freshness_score (restored) ──────────────────────────

class TestUriFreshnessScore(unittest.TestCase):

    def test_recent_uri_full_freshness(self):
        from curator.freshness import uri_freshness_score
        import time as _time
        recent_ts = int(_time.time()) - 86400
        uri = f'viking://resources/{recent_ts}_test_doc'
        score = uri_freshness_score(uri)
        self.assertEqual(score, 1.0)

    def test_old_uri_decayed(self):
        from curator.freshness import uri_freshness_score
        import time as _time
        old_ts = int(_time.time()) - 120 * 86400
        uri = f'viking://resources/{old_ts}_old_doc'
        score = uri_freshness_score(uri)
        self.assertLess(score, 1.0)
        self.assertGreater(score, 0.2)

    def test_very_old_uri_stale(self):
        from curator.freshness import uri_freshness_score
        import time as _time
        ancient_ts = int(_time.time()) - 400 * 86400
        uri = f'viking://resources/{ancient_ts}_ancient_doc'
        score = uri_freshness_score(uri)
        self.assertEqual(score, 0.1)

    def test_no_timestamp_returns_default(self):
        from curator.freshness import uri_freshness_score
        score = uri_freshness_score('viking://resources/no_timestamp_here')
        self.assertEqual(score, 0.5)


# ─── scan_duplicates (report-only) ───────────────────────────

class TestScanDuplicates(unittest.TestCase):

    def test_too_few_uris(self):
        from curator.dedup import scan_duplicates
        mock_ov = MagicMock()
        result = scan_duplicates(mock_ov, ["viking://a"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["duplicates"], [])

    def test_no_auto_delete(self):
        """scan_duplicates should never call delete/rm on the client."""
        from curator.dedup import scan_duplicates
        mock_ov = MagicMock()
        mock_ov.read.return_value = "some content " * 50
        scan_duplicates(mock_ov, ["viking://a", "viking://b"], max_checks=1)
        # Should never try to delete anything
        mock_ov.rm.assert_not_called()
        mock_ov.add_resource.assert_not_called()


# ─── load_context returns stage ──────────────────────────────

class TestLoadContextStage(unittest.TestCase):

    def test_returns_stage_none_on_empty(self):
        mock_ov = MagicMock()
        text, uris, stage = load_context(mock_ov, [], "test")
        self.assertEqual(stage, "none")

    def test_returns_stage_l0_on_high_score(self):
        mock_ov = MagicMock()
        items = [
            {"uri": "a", "score": 0.8, "abstract": "A detailed abstract about Docker deployment. " * 5},
            {"uri": "b", "score": 0.7, "abstract": "Another detailed abstract about container management. " * 5},
        ]
        text, uris, stage = load_context(mock_ov, items, "test")
        self.assertEqual(stage, "L0")


# ─── _log_query (Phase 1: query logging) ────────────────────

class TestLogQuery(unittest.TestCase):
    """Test query log writing."""

    def test_log_query_writes_jsonl(self):
        """_log_query should write a valid JSONL entry."""
        from curator.pipeline_v2 import _log_query
        with tempfile.TemporaryDirectory() as tmpdir:
            import curator.pipeline_v2 as pv2
            import curator.config as cfg
            old_data_path = cfg.DATA_PATH
            # Patch DATA_PATH at module level in pipeline_v2
            pv2_old = pv2.DATA_PATH if hasattr(pv2, 'DATA_PATH') else None
            try:
                # _log_query uses DATA_PATH from config import
                cfg.DATA_PATH = tmpdir
                # Re-import or patch the module-level reference
                import importlib
                importlib.reload(pv2)

                pv2._log_query(
                    query="test query",
                    coverage=0.75,
                    need_external=True,
                    reason="low_coverage",
                    used_uris=["viking://a", "viking://b"],
                    trace={"load_stage": "L1", "llm_calls": 1},
                )

                log_path = os.path.join(tmpdir, "query_log.jsonl")
                self.assertTrue(os.path.exists(log_path))
                with open(log_path, "r") as f:
                    lines = f.readlines()
                self.assertEqual(len(lines), 1)

                entry = json.loads(lines[0])
                self.assertEqual(entry["query"], "test query")
                self.assertAlmostEqual(entry["coverage"], 0.75, places=2)
                self.assertTrue(entry["external_triggered"])
                self.assertEqual(entry["reason"], "low_coverage")
                self.assertEqual(entry["used_uris"], ["viking://a", "viking://b"])
                self.assertEqual(entry["load_stage"], "L1")
                self.assertEqual(entry["llm_calls"], 1)
                self.assertIn("timestamp", entry)
            finally:
                cfg.DATA_PATH = old_data_path
                importlib.reload(pv2)

    def test_log_query_appends(self):
        """_log_query should append, not overwrite."""
        from curator.pipeline_v2 import _log_query
        with tempfile.TemporaryDirectory() as tmpdir:
            import curator.pipeline_v2 as pv2
            import curator.config as cfg
            old_data_path = cfg.DATA_PATH
            try:
                cfg.DATA_PATH = tmpdir
                import importlib
                importlib.reload(pv2)

                for i in range(3):
                    pv2._log_query(f"query_{i}", 0.5, False, "ok", [], {"load_stage": "L0", "llm_calls": 0})

                log_path = os.path.join(tmpdir, "query_log.jsonl")
                with open(log_path, "r") as f:
                    lines = f.readlines()
                self.assertEqual(len(lines), 3)
            finally:
                cfg.DATA_PATH = old_data_path
                importlib.reload(pv2)

    def test_log_query_failure_silent(self):
        """_log_query should not raise even if writing fails."""
        from curator.pipeline_v2 import _log_query
        with patch("builtins.open", side_effect=PermissionError("denied")):
            import curator.pipeline_v2 as pv2
            import curator.config as cfg
            old = cfg.DATA_PATH
            cfg.DATA_PATH = "/nonexistent/path"
            try:
                import importlib
                importlib.reload(pv2)
                # Should not raise
                pv2._log_query("q", 0.5, False, "ok", [], {"load_stage": "L0", "llm_calls": 0})
            finally:
                cfg.DATA_PATH = old
                importlib.reload(pv2)


# ─── analyze_weak (Phase 1: weakness analysis) ──────────────

class TestAnalyzeWeak(unittest.TestCase):
    """Test weakness analysis from query logs."""

    def _write_log(self, tmpdir, entries):
        log_path = os.path.join(tmpdir, "query_log.jsonl")
        with open(log_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_identifies_weak_topics(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from analyze_weak import analyze
        with tempfile.TemporaryDirectory() as tmpdir:
            # Same topic extracted ("redis cluster"), all triggering external search
            entries = [
                {"query": "redis cluster 部署", "coverage": 0.2, "external_triggered": True, "reason": "low_coverage"},
                {"query": "redis cluster 部署", "coverage": 0.3, "external_triggered": True, "reason": "low_coverage"},
                {"query": "redis cluster 部署", "coverage": 0.25, "external_triggered": True, "reason": "low_coverage"},
            ]
            self._write_log(tmpdir, entries)
            weak = analyze(tmpdir, min_queries=2)
            self.assertGreater(len(weak), 0)
            # At least one topic should have external_rate > 0.5
            self.assertTrue(any(t["external_rate"] > 0.5 for t in weak))

    def test_no_weak_when_coverage_good(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from analyze_weak import analyze
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = [
                {"query": "docker compose 部署", "coverage": 0.8, "external_triggered": False, "reason": "local_sufficient"},
                {"query": "docker compose 配置", "coverage": 0.9, "external_triggered": False, "reason": "local_sufficient"},
            ]
            self._write_log(tmpdir, entries)
            weak = analyze(tmpdir, min_queries=2)
            self.assertEqual(len(weak), 0)

    def test_min_queries_filter(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from analyze_weak import analyze
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = [
                {"query": "kubernetes helm", "coverage": 0.1, "external_triggered": True, "reason": "no_results"},
            ]
            self._write_log(tmpdir, entries)
            # min_queries=2, only 1 query → no weak topics
            weak = analyze(tmpdir, min_queries=2)
            self.assertEqual(len(weak), 0)
            # min_queries=1 → should find it
            weak = analyze(tmpdir, min_queries=1)
            self.assertGreater(len(weak), 0)

    def test_empty_log(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from analyze_weak import analyze
        with tempfile.TemporaryDirectory() as tmpdir:
            # No log file
            weak = analyze(tmpdir)
            self.assertEqual(len(weak), 0)

    def test_extract_keywords(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from analyze_weak import extract_keywords
        kws = extract_keywords("Redis 和 Memcached 怎么选")
        kw_lower = [k.lower() for k in kws]
        self.assertIn("redis", kw_lower)
        self.assertIn("memcached", kw_lower)
        # 停用词应被过滤
        self.assertNotIn("怎么", kw_lower)
        self.assertNotIn("和", kw_lower)


if __name__ == '__main__':
    unittest.main()
