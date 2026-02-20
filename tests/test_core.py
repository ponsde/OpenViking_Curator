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

    def test_wait_processed_calls_api(self):
        from curator.session_manager import OVClient
        client = OVClient("http://127.0.0.1:9100")
        with patch.object(client, '_post', return_value={"status": "ok"}) as mock_post:
            result = client.wait_processed(timeout=15)
            mock_post.assert_called_once_with(
                "/api/v1/system/wait",
                {"timeout": 15},
                timeout=25,
            )
            self.assertEqual(result["status"], "ok")


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


if __name__ == '__main__':
    unittest.main()
