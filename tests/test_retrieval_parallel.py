"""Tests for parallel L1/L2 loading in retrieval_v2.load_context()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from curator.backend_memory import InMemoryBackend
from curator.retrieval_v2 import load_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_items(*uri_score_abstract):
    """Build item list from (uri, score, abstract) tuples."""
    return [{"uri": u, "score": s, "abstract": a} for u, s, a in uri_score_abstract]


def _seed_backend(backend: InMemoryBackend, entries: list[tuple[str, str]]) -> dict[str, str]:
    """Ingest entries and return {title: uri} mapping.

    Each entry is (title, content).
    """
    mapping: dict[str, str] = {}
    for title, content in entries:
        uri = backend.ingest(content, title=title)
        mapping[title] = uri
    return mapping


# ---------------------------------------------------------------------------
# 2.5a: Parallel loading produces same results as serial would
# ---------------------------------------------------------------------------


class TestParallelL1Consistency:
    """Parallel L1 overview loading should produce results identical to serial."""

    def test_multiple_uris_loaded_in_order(self):
        """With >= 2 URIs, parallel L1 produces same blocks/uris/stage as serial."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("alpha", "A" * 60 + " alpha content overview section with enough text"),
                ("beta", "B" * 60 + " beta content overview section with enough text"),
                ("gamma", "C" * 60 + " gamma content overview section with enough text"),
            ],
        )

        items = _make_items(
            (uris["alpha"], 0.45, "A" * 60 + " alpha content"),
            (uris["beta"], 0.44, "B" * 60 + " beta content"),
            (uris["gamma"], 0.43, "C" * 60 + " gamma content"),
        )

        # L0 threshold is 0.62 by default, so 0.45 won't satisfy L0.
        # L1 threshold is 0.50 by default, so 0.45 won't satisfy L1 either.
        # This forces L2 fallback, but we want to test L1 loading happened.
        # Use max_l2=0 to force stop at L1 stage.
        ctx, used, stage = load_context(backend, items, "test query", max_l2=0)

        assert stage == "L1"
        assert len(used) == 3
        # Order must match score descending (alpha > beta > gamma)
        assert used[0] == uris["alpha"]
        assert used[1] == uris["beta"]
        assert used[2] == uris["gamma"]
        # All three sources should be in context
        for u in used:
            assert u in ctx

    def test_single_uri_no_thread_pool(self):
        """With exactly 1 URI, L1 stage must NOT create ThreadPoolExecutor."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("only", "X" * 60 + " only item overview with enough text for loading"),
            ],
        )

        items = _make_items(
            (uris["only"], 0.45, "X" * 60 + " only item abstract text"),
        )

        with patch("curator.retrieval_v2.concurrent.futures.ThreadPoolExecutor") as mock_pool:
            ctx, used, stage = load_context(backend, items, "test query", max_l2=0)

        mock_pool.assert_not_called()
        assert stage == "L1"
        assert len(used) == 1


class TestParallelL2Consistency:
    """Parallel L2 read loading should produce results identical to serial."""

    def test_multiple_uris_loaded_in_order(self):
        """With >= 2 URIs, parallel L2 produces same blocks/uris/stage as serial."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("doc1", "D" * 200 + " document one full content for deep reading"),
                ("doc2", "E" * 200 + " document two full content for deep reading"),
            ],
        )

        # Scores above L1_SUFFICIENT (0.50) so they qualify for L2
        # but below L0_SUFFICIENT (0.62) so L0 is not enough.
        # Override overview to return empty so L1 is NOT sufficient.
        items = _make_items(
            (uris["doc1"], 0.55, "short"),
            (uris["doc2"], 0.54, "short"),
        )

        backend.overview = lambda uri: ""  # force L1 to produce no blocks

        ctx, used, stage = load_context(backend, items, "test query", max_l2=2)

        assert stage == "L2"
        assert len(used) == 2
        assert used[0] == uris["doc1"]
        assert used[1] == uris["doc2"]

    def test_single_uri_no_thread_pool(self):
        """With exactly 1 L2-eligible URI, must NOT create ThreadPoolExecutor."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("sole", "S" * 200 + " sole document full content for deep reading"),
            ],
        )

        items = _make_items(
            (uris["sole"], 0.55, "short"),
        )

        backend.overview = lambda uri: ""  # force past L1

        with patch("curator.retrieval_v2.concurrent.futures.ThreadPoolExecutor") as mock_pool:
            ctx, used, stage = load_context(backend, items, "test query", max_l2=1)

        mock_pool.assert_not_called()
        assert stage == "L2"
        assert len(used) == 1


# ---------------------------------------------------------------------------
# 2.5b: Exception isolation — one failure must not affect others
# ---------------------------------------------------------------------------


class TestExceptionIsolation:
    """Failures in parallel overview/read must not affect other items."""

    def test_l1_one_overview_fails_others_survive(self):
        """If one overview() raises, other URIs still load fine."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("good1", "G" * 60 + " good item one overview with enough text here"),
                ("bad", "B" * 60 + " bad item overview that will fail to load"),
                ("good2", "H" * 60 + " good item two overview with enough text here"),
            ],
        )

        items = _make_items(
            (uris["good1"], 0.48, "G" * 60 + " good item one"),
            (uris["bad"], 0.47, "B" * 60 + " bad item content"),
            (uris["good2"], 0.46, "H" * 60 + " good item two"),
        )

        original_overview = backend.overview

        def flaky_overview(uri):
            if uri == uris["bad"]:
                raise RuntimeError("simulated overview failure")
            return original_overview(uri)

        backend.overview = flaky_overview

        ctx, used, stage = load_context(backend, items, "test query", max_l2=0)

        assert stage == "L1"
        # good1 and good2 should still be present
        assert uris["good1"] in used
        assert uris["good2"] in used
        # bad item falls back to abstract which has enough text, so it may still be present
        # The key assertion: no exception propagated and good items loaded

    def test_l2_one_read_fails_others_survive(self):
        """If one read() raises, other URIs still load fine."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("ok1", "O" * 200 + " ok document one full content for reading"),
                ("fail", "F" * 200 + " failing document content that will error"),
                ("ok2", "P" * 200 + " ok document two full content for reading"),
            ],
        )

        items = _make_items(
            (uris["ok1"], 0.55, "short"),
            (uris["fail"], 0.54, "short"),
            (uris["ok2"], 0.53, "short"),
        )

        original_read = backend.read

        def flaky_read(uri):
            if uri == uris["fail"]:
                raise RuntimeError("simulated read failure")
            return original_read(uri)

        backend.overview = lambda uri: ""  # force past L1
        backend.read = flaky_read

        ctx, used, stage = load_context(backend, items, "test query", max_l2=3)

        assert stage == "L2"
        assert uris["ok1"] in used
        assert uris["ok2"] in used
        # fail URI should not be in used (read returned None due to exception)
        assert uris["fail"] not in used


# ---------------------------------------------------------------------------
# 2.5c: Single URI optimization — verify no pool creation
# ---------------------------------------------------------------------------


class TestSingleUriOptimization:
    """Verify ThreadPoolExecutor is not created for single-URI cases."""

    def test_l1_zero_uris_no_pool(self):
        """Empty items should not create any thread pool."""
        backend = InMemoryBackend()

        with patch("curator.retrieval_v2.concurrent.futures.ThreadPoolExecutor") as mock_pool:
            ctx, used, stage = load_context(backend, [], "test", max_l2=0)

        mock_pool.assert_not_called()
        assert stage == "none"

    def test_l1_two_uris_creates_pool(self):
        """With 2+ URIs at L1 stage, ThreadPoolExecutor IS created."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("a", "A" * 60 + " first item overview with enough text content"),
                ("b", "B" * 60 + " second item overview with enough text content"),
            ],
        )

        items = _make_items(
            (uris["a"], 0.45, "A" * 60 + " first item abstract"),
            (uris["b"], 0.44, "B" * 60 + " second item abstract"),
        )

        # We patch at the module level but let the real ThreadPoolExecutor run
        call_log = []
        original_tpe = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor

        class TrackingTPE(original_tpe):
            def __init__(self, *args, **kwargs):
                call_log.append("created")
                super().__init__(*args, **kwargs)

        with patch("curator.retrieval_v2.concurrent.futures.ThreadPoolExecutor", TrackingTPE):
            load_context(backend, items, "test query", max_l2=0)

        assert len(call_log) >= 1, "ThreadPoolExecutor should be created for 2+ URIs"

    def test_l2_two_uris_creates_pool(self):
        """With 2+ URIs at L2 stage, ThreadPoolExecutor IS created."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("x", "X" * 200 + " x doc full content for deep reading test"),
                ("y", "Y" * 200 + " y doc full content for deep reading test"),
            ],
        )

        items = _make_items(
            (uris["x"], 0.55, "short"),
            (uris["y"], 0.54, "short"),
        )

        backend.overview = lambda uri: ""  # force past L1

        call_log = []
        original_tpe = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor

        class TrackingTPE(original_tpe):
            def __init__(self, *args, **kwargs):
                call_log.append("created")
                super().__init__(*args, **kwargs)

        with patch("curator.retrieval_v2.concurrent.futures.ThreadPoolExecutor", TrackingTPE):
            load_context(backend, items, "test query", max_l2=2)

        assert len(call_log) >= 1, "ThreadPoolExecutor should be created for 2+ L2 URIs"


# ---------------------------------------------------------------------------
# Regression: existing load_context behavior unchanged
# ---------------------------------------------------------------------------


class TestLoadContextRegression:
    """Ensure the parallelization doesn't break existing behavior."""

    def test_empty_items_returns_none_stage(self):
        backend = InMemoryBackend()
        ctx, used, stage = load_context(backend, [], "q")
        assert ctx == ""
        assert used == []
        assert stage == "none"

    def test_l0_sufficient_skips_l1(self):
        """High score + enough abstracts should return L0 without touching overview."""
        backend = InMemoryBackend()
        uris = _seed_backend(
            backend,
            [
                ("hi1", "H" * 60 + " high score item one abstract content"),
                ("hi2", "I" * 60 + " high score item two abstract content"),
            ],
        )

        items = _make_items(
            (uris["hi1"], 0.70, "H" * 60 + " high score item one abstract content"),
            (uris["hi2"], 0.68, "I" * 60 + " high score item two abstract content"),
        )

        overview_spy = MagicMock(side_effect=backend.overview)
        backend.overview = overview_spy

        ctx, used, stage = load_context(backend, items, "test")

        assert stage == "L0"
        overview_spy.assert_not_called()

    def test_function_signature_unchanged(self):
        """load_context accepts (backend, items, query, max_l2) and returns 3-tuple."""
        import inspect

        sig = inspect.signature(load_context)
        params = list(sig.parameters.keys())
        assert params == ["backend", "items", "query", "max_l2"]
