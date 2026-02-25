"""Tests for curator/dedup.py — URL hash and Jaccard similarity layers."""
import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile


class TestUrlHashes(unittest.TestCase):

    def test_extracts_http_urls(self):
        from curator.dedup import _url_hashes
        text = "See https://example.com/doc and http://other.org/page for details."
        hashes = _url_hashes(text)
        self.assertEqual(len(hashes), 2)

    def test_same_url_same_hash(self):
        from curator.dedup import _url_hashes
        h1 = _url_hashes("https://example.com/doc")
        h2 = _url_hashes("  https://example.com/doc  check it out")
        self.assertEqual(h1, h2)

    def test_different_urls_different_hashes(self):
        from curator.dedup import _url_hashes
        h1 = _url_hashes("https://example.com/a")
        h2 = _url_hashes("https://example.com/b")
        self.assertEqual(len(h1 & h2), 0)

    def test_empty_text_returns_empty(self):
        from curator.dedup import _url_hashes
        self.assertEqual(len(_url_hashes("")), 0)
        self.assertEqual(len(_url_hashes(None)), 0)

    def test_no_urls_returns_empty(self):
        from curator.dedup import _url_hashes
        self.assertEqual(len(_url_hashes("just plain text here")), 0)

    def test_short_urls_ignored(self):
        """URLs shorter than 8 chars after scheme are ignored (regex threshold)."""
        from curator.dedup import _url_hashes
        # 'http://x' has only 1 char after '://', should not match
        text = "see http://x go to https://example.com/valid-url"
        hashes = _url_hashes(text)
        # Only the long URL should match
        self.assertEqual(len(hashes), 1)


class TestUrlOverlap(unittest.TestCase):

    def test_overlap_returns_true(self):
        from curator.dedup import _url_hashes, _url_overlap
        h1 = _url_hashes("https://example.com/doc extra text")
        h2 = _url_hashes("totally different text but same source https://example.com/doc")
        self.assertTrue(_url_overlap(h1, h2))

    def test_no_overlap_returns_false(self):
        from curator.dedup import _url_hashes, _url_overlap
        h1 = _url_hashes("https://site-a.com/article")
        h2 = _url_hashes("https://site-b.org/post")
        self.assertFalse(_url_overlap(h1, h2))

    def test_empty_sets_return_false(self):
        from curator.dedup import _url_overlap
        self.assertFalse(_url_overlap(frozenset(), frozenset({"abc"})))
        self.assertFalse(_url_overlap(frozenset({"abc"}), frozenset()))
        self.assertFalse(_url_overlap(frozenset(), frozenset()))


class TestJaccardSimilarity(unittest.TestCase):

    def test_identical_texts_score_one(self):
        from curator.dedup import _jaccard_similarity
        text = "the quick brown fox jumps over the lazy dog"
        self.assertAlmostEqual(_jaccard_similarity(text, text), 1.0)

    def test_completely_different_texts_score_low(self):
        from curator.dedup import _jaccard_similarity
        a = "docker kubernetes container deployment"
        b = "renaissance painting baroque art museum"
        sim = _jaccard_similarity(a, b)
        self.assertLess(sim, 0.2)

    def test_partial_overlap_between_zero_and_one(self):
        from curator.dedup import _jaccard_similarity
        a = "python programming language tutorial beginner"
        b = "python programming advanced tutorial expert"
        sim = _jaccard_similarity(a, b)
        self.assertGreater(sim, 0.2)
        self.assertLess(sim, 1.0)

    def test_empty_strings_return_zero(self):
        from curator.dedup import _jaccard_similarity
        self.assertEqual(_jaccard_similarity("", "some text"), 0.0)
        self.assertEqual(_jaccard_similarity("some text", ""), 0.0)

    def test_order_invariant(self):
        """Jaccard should be symmetric."""
        from curator.dedup import _jaccard_similarity
        a = "machine learning neural network deep"
        b = "deep neural network learning algorithms"
        self.assertAlmostEqual(_jaccard_similarity(a, b), _jaccard_similarity(b, a))

    def test_better_than_sequencematcher_reordering(self):
        """Jaccard detects similarity when paragraphs are reordered."""
        from curator.dedup import _jaccard_similarity
        # Same words, different order — SequenceMatcher would score poorly
        a = "alpha beta gamma delta epsilon"
        b = "epsilon delta gamma beta alpha"
        sim = _jaccard_similarity(a, b)
        self.assertGreater(sim, 0.8)  # should be near 1.0 for identical word sets


class TestScanDuplicates(unittest.TestCase):

    def _make_backend(self, contents: dict):
        """Create a mock backend with given {uri: content} map."""
        mock = MagicMock()
        mock.read.side_effect = lambda uri: contents.get(uri, "")
        return mock

    def _tmp_log(self, tmp: str):
        """Return a patcher for curator.dedup.DEDUP_LOG_FILE → temp path."""
        return patch("curator.dedup.DEDUP_LOG_FILE", os.path.join(tmp, "dedup.json"))

    def test_url_hash_layer_detects_shared_source(self):
        """Two docs with shared source URL should be caught by Layer 1."""
        from curator.dedup import scan_duplicates
        contents = {
            "viking://a": "Intro text. https://example.com/original-source more text " * 20,
            "viking://b": "Different intro. https://example.com/original-source extra " * 20,
        }
        backend = self._make_backend(contents)
        with tempfile.TemporaryDirectory() as tmp:
            with self._tmp_log(tmp):
                result = scan_duplicates(backend, list(contents.keys()))
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(result["duplicates"][0]["method"], "url_hash")
        self.assertEqual(result["duplicates"][0]["similarity"], 1.0)

    def test_jaccard_layer_detects_high_text_similarity(self):
        """Two docs with same text (no URLs) should be caught by Layer 2."""
        from curator.dedup import scan_duplicates
        common = "machine learning neural network training dataset evaluation accuracy " * 30
        contents = {
            "viking://a": common,
            "viking://b": common + " some minor extra words",
        }
        backend = self._make_backend(contents)
        with tempfile.TemporaryDirectory() as tmp:
            with self._tmp_log(tmp):
                result = scan_duplicates(backend, list(contents.keys()))
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(result["duplicates"][0]["method"], "jaccard")

    def test_different_docs_not_flagged(self):
        """Truly different docs should produce no duplicates."""
        from curator.dedup import scan_duplicates
        contents = {
            "viking://a": "docker kubernetes deployment container orchestration " * 30,
            "viking://b": "baroque renaissance art painting museum history " * 30,
        }
        backend = self._make_backend(contents)
        with tempfile.TemporaryDirectory() as tmp:
            with self._tmp_log(tmp):
                result = scan_duplicates(backend, list(contents.keys()))
        self.assertEqual(result["duplicates"], [])

    def test_method_field_present_in_report(self):
        """Duplicate report must include 'method' field."""
        from curator.dedup import scan_duplicates
        common = "some repeated content words " * 50
        contents = {"viking://a": common, "viking://b": common}
        backend = self._make_backend(contents)
        with tempfile.TemporaryDirectory() as tmp:
            with self._tmp_log(tmp):
                result = scan_duplicates(backend, list(contents.keys()))
        if result["duplicates"]:
            self.assertIn("method", result["duplicates"][0])

    def test_no_auto_delete(self):
        """scan_duplicates must never call delete or rm."""
        from curator.dedup import scan_duplicates
        mock = MagicMock()
        mock.read.return_value = "content " * 50
        with tempfile.TemporaryDirectory() as tmp:
            with self._tmp_log(tmp):
                scan_duplicates(mock, ["viking://a", "viking://b"])
        mock.rm.assert_not_called()
        mock.delete.assert_not_called()

    def test_too_few_uris_returns_empty(self):
        """Single URI → no pairs to compare."""
        from curator.dedup import scan_duplicates
        result = scan_duplicates(MagicMock(), ["viking://a"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["duplicates"], [])
