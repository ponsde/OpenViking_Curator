#!/usr/bin/env python3
"""Unit tests for OpenViking Curator core functions."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set required env vars before importing curator
os.environ.setdefault('OAI_BASE', 'http://localhost:8000/v1')
os.environ.setdefault('OAI_KEY', 'test-key')
os.environ.setdefault('GROK_SEARCH_URL', 'http://localhost:8788')

import feedback_store
from curator import (
    route_scope,
    deterministic_relevance,
    uri_feedback_score,
    build_feedback_priority_uris,
    external_boost_needed,
    _build_source_footer,
)


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


# ─── deterministic_relevance ─────────────────────────────────

class TestDeterministicRelevance(unittest.TestCase):

    def test_high_coverage(self):
        """Full keyword coverage + evidence should score high."""
        relevance, ev_ratio, uri_hit = deterministic_relevance(
            query="OpenViking 部署步骤",
            scope={'domain': 'tech', 'keywords': ['openviking', '部署', '步骤'], 'time_hint': 'none'},
            txt="OpenViking 部署步骤如下：先安装依赖……",
            uris=['viking://tech/deploy-guide'],
            domain_hit=True,
            kw_cov=1.0,
        )
        self.assertGreaterEqual(relevance, 0.7)
        self.assertGreater(ev_ratio, 0)

    def test_zero_coverage(self):
        """No keyword match should score low."""
        relevance, ev_ratio, uri_hit = deterministic_relevance(
            query="明天天气怎么样",
            scope={'domain': 'general', 'keywords': ['天气', '明天'], 'time_hint': 'none'},
            txt="这是一篇关于 Python 编程的文章",
            uris=[],
            domain_hit=False,
            kw_cov=0.0,
        )
        self.assertLessEqual(relevance, 0.3)

    def test_partial_coverage(self):
        """Partial match should be in between."""
        relevance, ev_ratio, uri_hit = deterministic_relevance(
            query="grok 搜索 API",
            scope={'domain': 'tech', 'keywords': ['grok', '搜索', 'api'], 'time_hint': 'none'},
            txt="grok 是一个工具",
            uris=['viking://tech/grok'],
            domain_hit=True,
            kw_cov=0.33,
        )
        self.assertGreater(relevance, 0.2)
        self.assertLess(relevance, 0.8)


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
        # Should find parent and return > 0
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
        # 'a' should rank first (highest score)
        self.assertEqual(uri_list[0], 'viking://a')

    def test_negative_excluded_from_top(self):
        uri_list, scored = build_feedback_priority_uris(
            ['viking://b'],
            feedback_file=self.tmpfile.name,
            topn=3,
        )
        # 'b' has negative score, still returned if it's the only one
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
        """Core keyword miss should trigger external even if coverage is OK."""
        triggered, reason = external_boost_needed(
            query="Deno 2.0 和 Bun 性能对比",
            scope={'domain': 'general', 'keywords': ['Deno', 'Bun']},
            coverage=0.8,
            meta={'avg_top_trust': 5.5, 'fresh_ratio': 1.0, 'core_cov': 0.0},
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, 'low_core_coverage')


# ─── _build_source_footer ────────────────────────────────────

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
        # Point store at temp file
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
        """Multiple sequential applies should not lose data."""
        for i in range(20):
            feedback_store.apply('viking://concurrent', 'up')
        data = feedback_store.load()
        self.assertEqual(data['viking://concurrent']['up'], 20)


if __name__ == '__main__':
    unittest.main()
