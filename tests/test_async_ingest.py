"""Tests for async (fire-and-forget) ingest mode in pipeline_v2."""

import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")


class TestAsyncIngest(unittest.TestCase):
    """Verify CURATOR_ASYNC_INGEST=1 makes judge+ingest non-blocking."""

    def _mock_pipeline_deps(self):
        """Return common patches for pipeline_v2 module attributes."""
        return {
            "_init_session_manager": MagicMock(return_value=(MagicMock(), MagicMock())),
            "ov_retrieve": MagicMock(
                return_value={
                    "all_items": [{"uri": "a", "score": 0.2, "abstract": "short"}],
                    "memories": [],
                    "resources": [{"uri": "a", "score": 0.2, "abstract": "short"}],
                    "skills": [],
                }
            ),
            "assess_coverage": MagicMock(return_value=(0.2, True, "low_coverage")),
            "load_context": MagicMock(return_value=("local context", ["viking://a"], "L0")),
            "external_search": MagicMock(return_value="External search result content"),
            "capture_case": MagicMock(return_value=None),
            "validate_config": MagicMock(),
        }

    def test_async_off_calls_judge_synchronously(self):
        """When ASYNC_INGEST=0, judge_and_ingest is called before return."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        judge_called = threading.Event()

        def mock_judge(*a, **kw):
            judge_called.set()
            return {
                "pass": True,
                "trust": 8,
                "freshness": "current",
                "reason": "ok",
                "markdown": "# Test",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=mock_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "0"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                result = run("test query", backend=backend, auto_ingest=True)

        # Judge should have been called synchronously
        self.assertTrue(judge_called.is_set())
        # Meta should NOT have async flag
        self.assertFalse(result["meta"].get("async_ingest_pending", False))

    def test_async_on_returns_before_judge(self):
        """When ASYNC_INGEST=1, run() returns before judge completes."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        judge_started = threading.Event()
        judge_proceed = threading.Event()

        def slow_judge(*a, **kw):
            judge_started.set()
            judge_proceed.wait(timeout=5)  # block until test signals
            return {
                "pass": True,
                "trust": 8,
                "freshness": "current",
                "reason": "ok",
                "markdown": "# Async Test",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=slow_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                result = run("test query", backend=backend, auto_ingest=True)

        # run() returned while judge is still blocked
        self.assertTrue(result["meta"].get("async_ingest_pending", False))
        self.assertIn("external_text", result)
        self.assertEqual(result["external_text"], "External search result content")

        # Verify the background thread actually started
        self.assertTrue(judge_started.wait(timeout=2), "background judge thread did not start")

        # Let the background judge finish
        judge_proceed.set()
        # Give background thread time to complete
        time.sleep(0.5)

    def test_async_on_still_returns_external_text(self):
        """Async mode should include external_text in the result."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()

        def mock_judge(*a, **kw):
            return {
                "pass": True,
                "trust": 7,
                "freshness": "recent",
                "reason": "ok",
                "markdown": "# Content",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=mock_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                result = run("test query", backend=backend, auto_ingest=True)

        self.assertEqual(result["external_text"], "External search result content")

    def test_async_background_failure_does_not_crash(self):
        """If judge fails in background, no exception propagates."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()

        def failing_judge(*a, **kw):
            raise RuntimeError("LLM unavailable")

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=failing_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                # Should not raise
                result = run("test query", backend=backend, auto_ingest=True)

        self.assertTrue(result["meta"].get("async_ingest_pending", False))
        # Give background thread time to fail gracefully
        time.sleep(0.5)

    def test_async_off_when_no_external(self):
        """When coverage is sufficient, no external search, no async needed."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()

        patches = self._mock_pipeline_deps()
        # Override: coverage sufficient, no external needed
        patches["assess_coverage"] = MagicMock(return_value=(0.8, False, "local_sufficient"))

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                result = run("test query", backend=backend)

        # No external, no async
        self.assertFalse(result["meta"].get("async_ingest_pending", False))

    def test_async_respects_review_mode(self):
        """Async mode should NOT fire when auto_ingest=False (review mode)."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        judge_called = threading.Event()

        def mock_judge(*a, **kw):
            judge_called.set()
            return {
                "pass": True,
                "trust": 8,
                "freshness": "current",
                "reason": "ok",
                "markdown": "# Test",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=mock_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                # auto_ingest=False means review mode — judge still runs sync
                # because user wants to see the judge result
                run("test query", backend=backend, auto_ingest=False)

        # Judge should still be called (sync) since review mode needs results
        self.assertTrue(judge_called.is_set())


if __name__ == "__main__":
    unittest.main()
