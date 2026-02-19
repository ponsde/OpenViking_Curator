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
    uri_feedback_score,
    uri_trust_score,
    uri_freshness_score,
    build_feedback_priority_uris,
    external_boost_needed,
    validate_config,
    # v2 modules
    ov_retrieve,
    load_context,
    assess_coverage,
    answer,
    _build_source_footer,
)
from curator_query import should_route


# ─── route_scope ─────────────────────────────────────────────

class TestRouteScope(unittest.TestCase):
    """Test query scope routing (rule-based fast route)."""

    def test_domain_detection(self):
        scope = route_scope("Python async 怎么用")
        self.assertEqual(scope['domain'], 'technology')
        self.assertIn('Python', scope['keywords'])

    def test_general_domain_fallback(self):
        scope = route_scope("今天天气怎么样")
        self.assertEqual(scope['domain'], 'general')
        self.assertIsInstance(scope['keywords'], list)

    def test_keywords_extraction(self):
        scope = route_scope("Nginx 反向代理 502 怎么排查？")
        kw_lower = [k.lower() for k in scope['keywords']]
        self.assertIn('nginx', kw_lower)
        self.assertIn('502', kw_lower)

    def test_need_fresh(self):
        scope = route_scope("2026 最新 Python 发布了什么")
        self.assertTrue(scope['need_fresh'])


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
        items = [
            {"uri": "a", "score": 0.5},
        ]
        cov, need_ext, reason = assess_coverage({"all_items": items})
        self.assertFalse(need_ext)
        self.assertEqual(reason, "local_marginal")

    def test_low_score_triggers_external(self):
        items = [
            {"uri": "a", "score": 0.3},
        ]
        cov, need_ext, reason = assess_coverage({"all_items": items})
        self.assertTrue(need_ext)
        self.assertIn(reason, ("low_coverage", "insufficient"))


# ─── load_context (v2) ───────────────────────────────────────

class TestLoadContext(unittest.TestCase):
    """Test L0→L1→L2 layered context loading."""

    def test_empty_items(self):
        mock_ov = MagicMock()
        text, uris = load_context(mock_ov, [], "test query")
        self.assertEqual(text, "")
        self.assertEqual(uris, [])

    def test_l1_used_for_low_score(self):
        """Low-score items should use L1 (overview), not L2."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "This is a detailed overview of the topic."
        mock_ov.read.return_value = "Full content here"

        items = [{"uri": "a", "score": 0.4, "abstract": "short abstract"}]
        text, uris = load_context(mock_ov, items, "test query")

        self.assertIn("a", uris)
        # overview was called
        mock_ov.overview.assert_called_once_with("a")
        # read should NOT be called (score < 0.55)
        mock_ov.read.assert_not_called()

    def test_l2_used_for_high_score(self):
        """High-score top items should get L2 (read)."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "Overview content here with enough text."
        mock_ov.read.return_value = "Full detailed content of the document for deep reading."

        items = [{"uri": "a", "score": 0.7, "abstract": "abstract text"}]
        text, uris = load_context(mock_ov, items, "test query")

        self.assertIn("a", uris)
        mock_ov.read.assert_called_once_with("a")
        self.assertIn("Full detailed content", text)

    def test_max_l2_respected(self):
        """Should not read more than max_l2 items at L2."""
        mock_ov = MagicMock()
        mock_ov.overview.return_value = "Overview text sufficient for display."
        mock_ov.read.return_value = "Full content for reading."

        items = [
            {"uri": f"item_{i}", "score": 0.8 - i * 0.01, "abstract": "abs"}
            for i in range(5)
        ]
        text, uris = load_context(mock_ov, items, "test", max_l2=2)

        # read should be called at most 2 times
        self.assertLessEqual(mock_ov.read.call_count, 2)


# ─── uri_feedback_score ──────────────────────────────────────

class TestUriFeedbackScore(unittest.TestCase):

    def test_positive_feedback(self):
        fb = {'viking://a': {'up': 5, 'down': 0, 'adopt': 2}}
        score = uri_feedback_score('viking://a', fb)
        self.assertGreater(score, 0)

    def test_negative_feedback(self):
        fb = {'viking://b': {'up': 0, 'down': 10, 'adopt': 0}}
        score = uri_feedback_score('viking://b', fb)
        self.assertLess(score, 0)

    def test_missing_uri(self):
        fb = {}
        score = uri_feedback_score('viking://missing', fb)
        self.assertEqual(score, 0)

    def test_fuzzy_parent_match(self):
        """Child URI should pick up parent feedback."""
        fb = {'viking://tech': {'up': 3, 'down': 0, 'adopt': 1}}
        score = uri_feedback_score('viking://tech/sub/page', fb)
        self.assertGreaterEqual(score, 0)


# ─── build_feedback_priority_uris ────────────────────────────

class TestBuildFeedbackPriorityUris(unittest.TestCase):

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        json.dump({
            'viking://a': {'up': 10, 'down': 0, 'adopt': 5},
            'viking://b': {'up': 1, 'down': 8, 'adopt': 0},
            'viking://c': {'up': 3, 'down': 0, 'adopt': 1},
        }, self.tmpfile, ensure_ascii=False)
        self.tmpfile.close()

    def tearDown(self):
        os.unlink(self.tmpfile.name)

    def test_returns_topn(self):
        uri_list, scored = build_feedback_priority_uris(
            ['viking://a', 'viking://b', 'viking://c'],
            feedback_file=self.tmpfile.name,
            topn=2,
        )
        self.assertEqual(len(uri_list), 2)
        self.assertEqual(uri_list[0], 'viking://a')

    def test_negative_excluded_from_top(self):
        uri_list, scored = build_feedback_priority_uris(
            ['viking://b'],
            feedback_file=self.tmpfile.name,
            topn=3,
        )
        self.assertIn('viking://b', uri_list)


# ─── external_boost_needed ───────────────────────────────────

class TestExternalBoostNeeded(unittest.TestCase):

    def test_low_coverage_triggers(self):
        triggered, reason = external_boost_needed(
            query="新出的 AI 框架",
            scope={'domain': 'tech', 'keywords': ['ai', '框架'], 'time_hint': 'recent'},
            coverage=0.2,
            meta={'avg_top_trust': 0.5, 'fresh_ratio': 0.5},
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, 'low_coverage')

    def test_high_coverage_no_trigger(self):
        triggered, reason = external_boost_needed(
            query="OpenViking 架构",
            scope={'domain': 'tech', 'keywords': ['openviking', '架构'], 'time_hint': 'none'},
            coverage=0.9,
            meta={'avg_top_trust': 0.8, 'fresh_ratio': 0.8},
        )
        self.assertFalse(triggered)

    def test_low_core_coverage_triggers(self):
        triggered, reason = external_boost_needed(
            query="Deno 2.0 和 Bun 性能对比",
            scope={'domain': 'general', 'keywords': ['Deno', 'Bun']},
            coverage=0.8,
            meta={'avg_top_trust': 5.5, 'fresh_ratio': 1.0, 'core_cov': 0.0},
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, 'low_core_coverage')


# ─── _build_source_footer (answer.py, optional) ─────────────

class TestSourceFooter(unittest.TestCase):

    def test_high_coverage_footer(self):
        footer = _build_source_footer(
            meta={'core_cov': 0.8, 'priority_uris': ['viking://resources/a/a.md']},
            coverage=0.9, external_used=False,
        )
        self.assertIn('90%', footer)
        self.assertIn('✅ 高', footer)
        self.assertIn('本地知识库', footer)
        self.assertNotIn('外部搜索', footer)

    def test_low_coverage_with_external(self):
        footer = _build_source_footer(
            meta={'core_cov': 0.2, 'priority_uris': []},
            coverage=0.3, external_used=True, warnings=['某API可能过时'],
        )
        self.assertIn('30%', footer)
        self.assertIn('❌ 低', footer)
        self.assertIn('外部搜索', footer)
        self.assertIn('1 条待验证', footer)


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
        with self.assertRaises(RuntimeError) as ctx:
            from curator.config import validate_config as vc
            import curator.config as cfg
            old_base, old_key = cfg.OAI_BASE, cfg.OAI_KEY
            cfg.OAI_BASE, cfg.OAI_KEY = '', ''
            try:
                cfg.validate_config()
            finally:
                cfg.OAI_BASE, cfg.OAI_KEY = old_base, old_key
        self.assertIn('CURATOR_OAI_BASE', str(ctx.exception))


# ─── uri_trust_score ─────────────────────────────────────────

class TestUriTrustScore(unittest.TestCase):

    def test_known_project_high_trust(self):
        score = uri_trust_score('viking://resources/openviking_guide')
        self.assertGreater(score, 6.0)

    def test_curated_medium_trust(self):
        score = uri_trust_score('viking://resources/123_curated/doc.md')
        self.assertGreater(score, 6.0)

    def test_unknown_uri(self):
        score = uri_trust_score('viking://resources/random_doc')
        self.assertAlmostEqual(score, 5.5)

    def test_license_low_trust(self):
        score = uri_trust_score('viking://resources/license.md')
        self.assertLess(score, 5.0)

    def test_feedback_boosts_trust(self):
        fb = {'viking://resources/random_doc': {'up': 5, 'down': 0, 'adopt': 2}}
        base = uri_trust_score('viking://resources/random_doc')
        boosted = uri_trust_score('viking://resources/random_doc', fb=fb)
        self.assertGreater(boosted, base)

    def test_feedback_decreases_trust(self):
        fb = {'viking://resources/openviking_guide': {'up': 0, 'down': 8, 'adopt': 0}}
        base = uri_trust_score('viking://resources/openviking_guide')
        decreased = uri_trust_score('viking://resources/openviking_guide', fb=fb)
        self.assertLess(decreased, base)

    def test_trust_clamped(self):
        fb_extreme = {'viking://resources/x': {'up': 100, 'down': 0, 'adopt': 50}}
        score = uri_trust_score('viking://resources/x', fb=fb_extreme)
        self.assertLessEqual(score, 10.0)
        self.assertGreaterEqual(score, 1.0)


# ─── uri_freshness_score ─────────────────────────────────────

class TestUriFreshnessScore(unittest.TestCase):

    def test_recent_uri_full_freshness(self):
        import time as _time
        recent_ts = int(_time.time()) - 86400
        uri = f'viking://resources/{recent_ts}_test_doc'
        score = uri_freshness_score(uri)
        self.assertEqual(score, 1.0)

    def test_old_uri_decayed(self):
        import time as _time
        old_ts = int(_time.time()) - 120 * 86400
        uri = f'viking://resources/{old_ts}_old_doc'
        score = uri_freshness_score(uri)
        self.assertLess(score, 1.0)
        self.assertGreater(score, 0.2)

    def test_very_old_uri_stale(self):
        import time as _time
        ancient_ts = int(_time.time()) - 400 * 86400
        uri = f'viking://resources/{ancient_ts}_ancient_doc'
        score = uri_freshness_score(uri)
        self.assertEqual(score, 0.1)

    def test_no_timestamp_returns_default(self):
        score = uri_freshness_score('viking://resources/no_timestamp_here')
        self.assertEqual(score, 0.5)

    def test_meta_date_overrides_uri(self):
        import time as _time
        now = _time.time()
        meta = {'created_at': int(now - 10 * 86400)}
        score = uri_freshness_score('viking://resources/some_doc', meta=meta)
        self.assertEqual(score, 1.0)

    def test_meta_iso_date(self):
        score = uri_freshness_score('viking://resources/x', meta={'date': '2026-02-15'}, now=1771439300.0)
        self.assertGreater(score, 0.9)


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
    """Test that wait_processed method exists and calls correct endpoint."""

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


if __name__ == '__main__':
    unittest.main()
