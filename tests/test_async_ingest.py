"""Tests for async (fire-and-forget) ingest mode in pipeline_v2."""

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")


class TestAsyncIngest(unittest.TestCase):
    """Verify CURATOR_ASYNC_INGEST=1 makes judge+ingest non-blocking."""

    def _mock_pipeline_deps(self, async_ingest: bool = True):
        """Return common patches for pipeline_v2 module attributes."""
        return {
            "ASYNC_INGEST": async_ingest,
            "backend_retrieve": MagicMock(
                return_value={
                    "all_items": [{"uri": "a", "score": 0.2, "abstract": "short"}],
                    "all_items_raw": [{"uri": "a", "score": 0.2, "abstract": "short"}],
                    "memories": [],
                    "resources": [{"uri": "a", "score": 0.2, "abstract": "short"}],
                    "skills": [],
                    "query_plan": None,
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

        patches = self._mock_pipeline_deps(async_ingest=False)
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
                self.assertTrue(judge_started.wait(timeout=3), "background judge thread did not start")

                # Let the background judge finish inside patch scope
                judge_proceed.set()
                time.sleep(0.3)

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
                # Wait for background thread to complete inside patch scope
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

    def test_async_failure_writes_to_failure_log(self):
        """Background judge failure should be logged to async_ingest_failures.jsonl."""
        import tempfile

        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        failure_done = threading.Event()

        def failing_judge(*a, **kw):
            raise RuntimeError("LLM timeout")

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=failing_judge)

        with tempfile.TemporaryDirectory() as tmpdir:
            env_vars = {
                "CURATOR_ASYNC_INGEST": "1",
                "CURATOR_DATA_PATH": tmpdir,
            }
            with patch.dict(os.environ, env_vars):
                with patch("curator.pipeline_v2.DATA_PATH", tmpdir):
                    with patch.multiple("curator.pipeline_v2", **patches):
                        # Patch _log_async_failure to signal when done
                        original_log = None

                        import curator.pipeline_v2 as _p2

                        original_log = _p2._log_async_failure

                        def _patched_log(*args, **kwargs):
                            original_log(*args, **kwargs)
                            failure_done.set()

                        with patch.object(_p2, "_log_async_failure", side_effect=_patched_log):
                            from curator.pipeline_v2 import run

                            run("failing query", backend=backend, auto_ingest=True)

                            # Wait inside patch scope so background thread keeps mocks
                            self.assertTrue(failure_done.wait(timeout=3), "failure log was not written")
                            log_path = Path(tmpdir) / "async_ingest_failures.jsonl"
                            self.assertTrue(log_path.exists(), "failure log file should exist")
                            lines = log_path.read_text().strip().split("\n")
                            entry = json.loads(lines[-1])
                            self.assertEqual(entry["query"], "failing query")
                            self.assertIn("LLM timeout", entry["error"])

    def test_concurrent_async_runs_serialized(self):
        """Multiple concurrent async ingest runs should not overlap (lock)."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        execution_log = []  # track enter/exit of judge calls
        lock_for_test = threading.Lock()
        both_done = threading.Event()
        call_count = 0

        def tracked_judge(*a, **kw):
            nonlocal call_count
            with lock_for_test:
                execution_log.append("enter")
            time.sleep(0.2)
            with lock_for_test:
                execution_log.append("exit")
                call_count += 1
                if call_count >= 2:
                    both_done.set()
            return {
                "pass": False,  # pass=False so no ingest_markdown_v2 is called
                "trust": 3,
                "freshness": "unknown",
                "reason": "test",
                "markdown": "",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=tracked_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                # Fire two async runs nearly simultaneously
                run("query 1", backend=backend, auto_ingest=True)
                run("query 2", backend=backend, auto_ingest=True)

                # Wait for both background threads inside patch scope
                self.assertTrue(both_done.wait(timeout=5), "both background threads should complete")

        # Both should have executed (2 enters, 2 exits)
        with lock_for_test:
            self.assertEqual(execution_log.count("enter"), 2)
            self.assertEqual(execution_log.count("exit"), 2)

        # Because of _ingest_lock, the second 'enter' should come after first 'exit'
        # Pattern should be: enter, exit, enter, exit (serialized)
        # NOT: enter, enter, exit, exit (overlapping)
        with lock_for_test:
            first_exit = execution_log.index("exit")
            second_enter_positions = [i for i, x in enumerate(execution_log) if x == "enter"]
            if len(second_enter_positions) >= 2:
                self.assertGreaterEqual(
                    second_enter_positions[1],
                    first_exit,
                    "Second judge should start after first judge exits (serialized by lock)",
                )

    def test_async_ingest_internal_failure_logged(self):
        """When ingest_markdown_v2 fails inside _do_judge_ingest (async), it should be logged."""
        import tempfile

        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        failure_done = threading.Event()

        def mock_judge(*a, **kw):
            return {
                "pass": True,
                "trust": 8,
                "freshness": "current",
                "reason": "ok",
                "markdown": "# Content",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        def failing_ingest(*a, **kw):
            raise RuntimeError("backend write error")

        patches = self._mock_pipeline_deps()
        patches["judge_and_ingest"] = MagicMock(side_effect=mock_judge)

        with tempfile.TemporaryDirectory() as tmpdir:
            env_vars = {"CURATOR_ASYNC_INGEST": "1", "CURATOR_DATA_PATH": tmpdir}
            with patch.dict(os.environ, env_vars):
                with patch("curator.pipeline_v2.DATA_PATH", tmpdir):
                    with patch.multiple("curator.pipeline_v2", **patches):
                        import curator.pipeline_v2 as _p2

                        original_log_fn = _p2._log_async_failure

                        def _signal_log(*args, **kwargs):
                            original_log_fn(*args, **kwargs)
                            failure_done.set()

                        with patch.object(_p2, "_log_async_failure", side_effect=_signal_log):
                            with patch("curator.review.ingest_markdown_v2", side_effect=failing_ingest):
                                from curator.pipeline_v2 import run

                                run("ingest fail query", backend=backend, auto_ingest=True)

                                # Wait inside patch scope so background thread still has mocks active
                                self.assertTrue(
                                    failure_done.wait(timeout=3),
                                    "ingest internal failure should be logged",
                                )
                                log_path = Path(tmpdir) / "async_ingest_failures.jsonl"
                                self.assertTrue(log_path.exists())
                                entry = json.loads(log_path.read_text().strip().split("\n")[-1])
                                self.assertIn("backend write error", entry["error"])

    def test_async_need_fresh_path(self):
        """Async mode with need_fresh=True should still cross_validate in background."""
        from curator.backend_memory import InMemoryBackend

        backend = InMemoryBackend()
        cv_called = threading.Event()

        def mock_cv(query, text, scope):
            cv_called.set()
            return {"validated": text, "warnings": ["stale data"]}

        def mock_judge(*a, **kw):
            return {
                "pass": True,
                "trust": 7,
                "freshness": "recent",
                "reason": "ok",
                "markdown": "# Fresh",
                "has_conflict": False,
                "conflict_summary": "",
                "conflict_points": [],
            }

        patches = self._mock_pipeline_deps()
        # Override: need_fresh=True in scope
        patches["assess_coverage"] = MagicMock(return_value=(0.2, True, "need_fresh"))

        # Make route_scope return need_fresh
        def mock_route(q):
            return {"domain": "tech", "need_fresh": True}

        patches["route_scope"] = MagicMock(side_effect=mock_route)
        patches["cross_validate"] = MagicMock(side_effect=mock_cv)
        patches["judge_and_ingest"] = MagicMock(side_effect=mock_judge)

        with patch.dict(os.environ, {"CURATOR_ASYNC_INGEST": "1"}):
            with patch.multiple("curator.pipeline_v2", **patches):
                from curator.pipeline_v2 import run

                result = run("fresh query", backend=backend, auto_ingest=True)

                self.assertTrue(result["meta"].get("async_ingest_pending", False))

                # cross_validate should have been called in the background thread
                self.assertTrue(cv_called.wait(timeout=3), "cross_validate should be called in async path")


if __name__ == "__main__":
    unittest.main()
