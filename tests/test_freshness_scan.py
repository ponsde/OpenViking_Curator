#!/usr/bin/env python3
"""Unit tests for scripts/freshness_scan.py core logic."""
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required env vars before importing
os.environ.setdefault('OAI_BASE', 'http://localhost:8000/v1')
os.environ.setdefault('OAI_KEY', 'test-key')
os.environ.setdefault('GROK_SEARCH_URL', 'http://localhost:8788')

from scripts.freshness_scan import (
    parse_curator_meta,
    score_resource,
    categorize,
    extract_urls_from_content,
    extract_topic,
    generate_json_report,
    FRESH_THRESHOLD,
    AGING_THRESHOLD,
)


class TestParseCuratorMeta(unittest.TestCase):

    def test_standard_meta(self):
        content = "<!-- curator_meta: ingested=2026-01-15 freshness=current ttl_days=180 -->\n<!-- review_after: 2026-07-14 -->\n\n# Title"
        meta = parse_curator_meta(content)
        self.assertEqual(meta["ingested"], "2026-01-15")
        self.assertEqual(meta["freshness"], "current")
        self.assertEqual(meta["ttl_days"], "180")
        self.assertEqual(meta["review_after"], "2026-07-14")

    def test_no_meta(self):
        content = "# Just a plain document\n\nNo metadata here."
        meta = parse_curator_meta(content)
        self.assertEqual(meta, {})

    def test_empty_content(self):
        meta = parse_curator_meta("")
        self.assertEqual(meta, {})

    def test_meta_only_review_after(self):
        content = "<!-- review_after: 2025-12-31 -->\nsome content"
        meta = parse_curator_meta(content)
        self.assertEqual(meta["review_after"], "2025-12-31")

    def test_partial_meta(self):
        content = "<!-- curator_meta: ingested=2026-02-01 freshness=unknown -->\n"
        meta = parse_curator_meta(content)
        self.assertEqual(meta["ingested"], "2026-02-01")
        self.assertEqual(meta["freshness"], "unknown")
        self.assertNotIn("ttl_days", meta)


class TestScoreResource(unittest.TestCase):

    def test_recent_resource_is_fresh(self):
        ts = int(time.time()) - 86400 * 5  # 5 days old
        res = {"uri": f"viking://resources/{ts}_test", "abstract": "test"}
        scored = score_resource(res)
        self.assertEqual(scored["category"], "fresh")
        self.assertGreaterEqual(scored["score"], FRESH_THRESHOLD)

    def test_old_resource_is_stale(self):
        ts = int(time.time()) - 86400 * 400  # 400 days old
        res = {"uri": f"viking://resources/{ts}_old", "abstract": "old stuff"}
        scored = score_resource(res)
        self.assertEqual(scored["category"], "stale")
        self.assertLess(scored["score"], AGING_THRESHOLD)

    def test_medium_age_is_aging(self):
        ts = int(time.time()) - 86400 * 100  # 100 days old
        res = {"uri": f"viking://resources/{ts}_mid", "abstract": "mid age"}
        scored = score_resource(res)
        self.assertEqual(scored["category"], "aging")
        self.assertGreaterEqual(scored["score"], AGING_THRESHOLD)
        self.assertLess(scored["score"], FRESH_THRESHOLD)

    def test_no_timestamp_defaults_to_aging(self):
        res = {"uri": "viking://resources/no_timestamp_here", "abstract": "unknown age"}
        scored = score_resource(res)
        # uri_freshness_score returns 0.5 for unknown → aging
        self.assertEqual(scored["category"], "aging")
        self.assertEqual(scored["score"], 0.5)

    def test_review_expired(self):
        ts = int(time.time()) - 86400 * 200
        res = {"uri": f"viking://resources/{ts}_expired", "abstract": ""}
        content = "<!-- curator_meta: ingested=2025-06-01 freshness=current ttl_days=180 -->\n<!-- review_after: 2025-12-01 -->\n"
        scored = score_resource(res, content)
        self.assertTrue(scored["review_expired"])

    def test_review_not_expired(self):
        ts = int(time.time()) - 86400 * 10
        res = {"uri": f"viking://resources/{ts}_fresh", "abstract": ""}
        content = "<!-- review_after: 2030-12-01 -->\n"
        scored = score_resource(res, content)
        self.assertFalse(scored["review_expired"])

    def test_meta_used_for_scoring(self):
        """When meta has date info, it should influence scoring."""
        res = {"uri": "viking://resources/no_ts_doc", "abstract": ""}
        # Set created_at to very recent
        recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
        content = f"<!-- curator_meta: ingested={datetime.date.today().isoformat()} freshness=current ttl_days=180 -->\n"
        scored = score_resource(res, content)
        # The ingested date in meta should be picked up
        self.assertIn("ingested", scored["meta"])


class TestCategorize(unittest.TestCase):

    def test_empty(self):
        cats = categorize([])
        self.assertEqual(cats, {"fresh": [], "aging": [], "stale": []})

    def test_proper_categorization(self):
        items = [
            {"category": "fresh", "uri": "a"},
            {"category": "aging", "uri": "b"},
            {"category": "stale", "uri": "c"},
            {"category": "fresh", "uri": "d"},
        ]
        cats = categorize(items)
        self.assertEqual(len(cats["fresh"]), 2)
        self.assertEqual(len(cats["aging"]), 1)
        self.assertEqual(len(cats["stale"]), 1)


class TestExtractUrls(unittest.TestCase):

    def test_basic_urls(self):
        content = "Visit https://example.com and http://foo.bar/path?q=1 for more."
        urls = extract_urls_from_content(content)
        self.assertIn("https://example.com", urls)
        self.assertIn("http://foo.bar/path?q=1", urls)

    def test_no_urls(self):
        self.assertEqual(extract_urls_from_content("no urls here"), [])

    def test_empty_content(self):
        self.assertEqual(extract_urls_from_content(""), [])

    def test_deduplication(self):
        content = "https://a.com and https://a.com again"
        urls = extract_urls_from_content(content)
        self.assertEqual(len(urls), 1)

    def test_trailing_punctuation_stripped(self):
        content = "See https://example.com/path. Also https://foo.com/bar,"
        urls = extract_urls_from_content(content)
        self.assertIn("https://example.com/path", urls)
        self.assertIn("https://foo.com/bar", urls)

    def test_short_urls_filtered(self):
        content = "http://x is too short"
        urls = extract_urls_from_content(content)
        self.assertEqual(len(urls), 0)


class TestExtractTopic(unittest.TestCase):

    def test_from_abstract(self):
        topic = extract_topic("viking://resources/123_test", "Docker deployment guide for production")
        self.assertEqual(topic, "Docker deployment guide for production")

    def test_from_uri_fallback(self):
        topic = extract_topic("viking://resources/123_docker_deploy", "")
        self.assertEqual(topic, "docker deploy")

    def test_abstract_priority_over_content(self):
        topic = extract_topic("viking://x", "Abstract text", "Content text")
        self.assertEqual(topic, "Abstract text")

    def test_long_abstract_truncated(self):
        long_text = "A" * 200
        topic = extract_topic("viking://x", long_text)
        self.assertLessEqual(len(topic), 100)


class TestGenerateJsonReport(unittest.TestCase):

    def test_basic_report_structure(self):
        cats = {
            "fresh": [{"uri": "a", "score": 0.9, "category": "fresh"}],
            "aging": [],
            "stale": [{"uri": "b", "score": 0.2, "category": "stale"}],
        }
        report = generate_json_report(cats)
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["fresh"], 1)
        self.assertEqual(report["summary"]["stale"], 1)
        self.assertIn("scan_date", report)
        self.assertIn("resources", report)

    def test_with_url_results(self):
        cats = {"fresh": [], "aging": [], "stale": []}
        url_results = {
            "viking://a": [
                {"url": "http://ok.com", "ok": True, "status": 200},
                {"url": "http://broken.com", "ok": False, "status": 404},
            ],
        }
        report = generate_json_report(cats, url_results=url_results)
        self.assertIn("url_checks", report)
        self.assertEqual(report["url_checks"]["checked_resources"], 1)
        self.assertEqual(report["url_checks"]["total_urls_checked"], 2)
        self.assertIn("viking://a", report["url_checks"]["broken_urls"])

    def test_with_actions(self):
        cats = {"fresh": [], "aging": [], "stale": []}
        actions = [{"uri": "x", "topic": "test", "action": "re-searched", "ingested": True}]
        report = generate_json_report(cats, actions=actions)
        self.assertIn("actions", report)
        self.assertEqual(len(report["actions"]), 1)

    def test_empty_report(self):
        cats = {"fresh": [], "aging": [], "stale": []}
        report = generate_json_report(cats)
        self.assertEqual(report["summary"]["total"], 0)
        self.assertNotIn("url_checks", report)
        self.assertNotIn("actions", report)


class TestCheckUrl(unittest.TestCase):

    def test_check_url_function_signature(self):
        """check_url should accept url and optional timeout."""
        from scripts.freshness_scan import check_url
        # Don't actually hit the network, just verify the function exists
        # and returns the right structure
        with patch('scripts.freshness_scan.urllib.request.urlopen') as mock_open:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            result = check_url("https://example.com")
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], 200)

    def test_check_url_handles_error(self):
        from scripts.freshness_scan import check_url
        with patch('scripts.freshness_scan.urllib.request.urlopen', side_effect=Exception("timeout")):
            result = check_url("https://nonexistent.example.com")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], 0)
            self.assertIn("error", result)


class TestIngestMarkdownV2Meta(unittest.TestCase):
    """Verify ingest_markdown_v2 writes correct curator_meta (Task 3.2)."""

    def test_meta_fields_present(self):
        """Ensure ingested, freshness, ttl_days, review_after all written."""
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('curator.review.CURATED_DIR', tmpdir):
                ingest_markdown_v2(backend, "test_doc", "# Content", freshness="current")

            # Find the written file
            files = list(os.listdir(tmpdir))
            self.assertEqual(len(files), 1)
            content = open(os.path.join(tmpdir, files[0])).read()

            self.assertIn("curator_meta:", content)
            self.assertIn("ingested=", content)
            self.assertIn("freshness=current", content)
            self.assertIn("ttl_days=180", content)
            self.assertIn("review_after:", content)

    def test_ttl_varies_by_freshness(self):
        """TTL should differ based on freshness level."""
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        for freshness, expected_ttl in [("current", 180), ("recent", 90), ("unknown", 60), ("outdated", 0)]:
            backend = InMemoryBackend()

            with tempfile.TemporaryDirectory() as tmpdir:
                with patch('curator.review.CURATED_DIR', tmpdir):
                    ingest_markdown_v2(backend, f"test_{freshness}", "# Content", freshness=freshness)
                files = list(os.listdir(tmpdir))
                content = open(os.path.join(tmpdir, files[0])).read()
                self.assertIn(f"ttl_days={expected_ttl}", content, f"Failed for freshness={freshness}")

    def test_metadata_contains_version_source_urls_and_quality_feedback(self):
        """ingest metadata should include version/source_urls/quality_feedback."""
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()

        md = "# Doc\nSee https://example.com/a and https://example.com/b"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('curator.review.CURATED_DIR', tmpdir):
                out = ingest_markdown_v2(
                    backend,
                    "meta_doc",
                    md,
                    freshness="recent",
                    source_urls=["https://example.com/a", "https://example.com/a"],
                    quality_feedback={"judge_trust": 8, "judge_reason": "ok"},
                )

        uri = out.get("root_uri")
        self.assertTrue(uri)
        rec = backend._store.get(uri)
        self.assertIsNotNone(rec)
        metadata = rec.get("metadata", {})

        self.assertIn("version", metadata)
        self.assertEqual(metadata.get("source_urls"), ["https://example.com/a"])
        self.assertEqual(metadata.get("quality_feedback", {}).get("judge_trust"), 8)

    def test_source_urls_none_falls_back_to_markdown_extraction(self):
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        md = "ref: https://example.com/x and https://example.com/y"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('curator.review.CURATED_DIR', tmpdir):
                out = ingest_markdown_v2(backend, "meta_doc", md, source_urls=None)

        rec = backend._store.get(out["root_uri"])
        metadata = rec.get("metadata", {})
        self.assertEqual(metadata.get("source_urls"), ["https://example.com/x", "https://example.com/y"])

    def test_source_urls_empty_list_respected(self):
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        md = "ref: https://example.com/x"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('curator.review.CURATED_DIR', tmpdir):
                out = ingest_markdown_v2(backend, "meta_doc", md, source_urls=[])

        rec = backend._store.get(out["root_uri"])
        metadata = rec.get("metadata", {})
        self.assertEqual(metadata.get("source_urls"), [])

    def test_quality_feedback_non_dict_becomes_empty_dict(self):
        from curator.review import ingest_markdown_v2
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('curator.review.CURATED_DIR', tmpdir):
                out = ingest_markdown_v2(backend, "meta_doc", "# x", quality_feedback="bad-type")

        rec = backend._store.get(out["root_uri"])
        metadata = rec.get("metadata", {})
        self.assertEqual(metadata.get("quality_feedback"), {})


if __name__ == "__main__":
    unittest.main()
