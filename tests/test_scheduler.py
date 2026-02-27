"""Tests for curator.scheduler — background maintenance jobs."""

import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

import curator.scheduler as sched

# ── Helpers ──


def _mock_run_fn(results: list | None = None):
    """Return a callable that records calls and optionally returns pre-set results."""
    calls = []

    def _fn(query, **kwargs):
        calls.append(query)
        return (results or [{}])[len(calls) - 1] if results else {}

    _fn.calls = calls
    return _fn


# ── _run_freshen ──


class TestRunFreshen:
    def test_no_resources_returns_zeros(self):
        backend = MagicMock()
        backend.list_resources.return_value = []
        result = sched._run_freshen(_backend=backend, _run_fn=_mock_run_fn())
        assert result == {"checked": 0, "stale": 0, "re_searched": 0}
        backend.abstract.assert_not_called()

    def test_fresh_resources_not_re_searched(self, monkeypatch):
        # URI with recent timestamp → fresh
        recent_uri = f"viking://resources/{int(__import__('time').time())}_fresh"
        backend = MagicMock()
        backend.list_resources.return_value = [recent_uri]
        run_fn = _mock_run_fn()
        result = sched._run_freshen(_backend=backend, _run_fn=run_fn)
        assert result["checked"] == 1
        assert result["stale"] == 0
        assert result["re_searched"] == 0
        assert run_fn.calls == []

    def test_stale_resources_are_re_searched(self, monkeypatch):
        # URI with old timestamp → stale (score < 0.4)
        old_uri = "viking://resources/1640000000_old_topic"  # 2022 timestamp
        backend = MagicMock()
        backend.list_resources.return_value = [old_uri]
        backend.abstract.return_value = "Some topic abstract"
        run_fn = _mock_run_fn()
        result = sched._run_freshen(_backend=backend, _run_fn=run_fn)
        assert result["checked"] == 1
        assert result["stale"] == 1
        assert result["re_searched"] == 1
        assert len(run_fn.calls) == 1
        assert "Some topic abstract" in run_fn.calls[0]

    def test_uses_uri_slug_when_abstract_empty(self):
        old_uri = "viking://resources/1640000000_my_topic_slug"
        backend = MagicMock()
        backend.list_resources.return_value = [old_uri]
        backend.abstract.return_value = ""
        run_fn = _mock_run_fn()
        sched._run_freshen(_backend=backend, _run_fn=run_fn)
        # URI last segment has _ replaced by spaces
        assert "my topic slug" in run_fn.calls[0]

    def test_re_search_error_does_not_abort(self):
        old_uri = "viking://resources/1640000000_topic_a"
        backend = MagicMock()
        backend.list_resources.return_value = [old_uri]
        backend.abstract.return_value = "topic a"

        def _failing_run(query, **kwargs):
            raise RuntimeError("LLM down")

        result = sched._run_freshen(_backend=backend, _run_fn=_failing_run)
        assert result["re_searched"] == 0
        assert "error" not in result  # job-level error is absent; item failure is logged

    def test_backend_error_returns_error_dict(self):
        backend = MagicMock()
        backend.list_resources.side_effect = RuntimeError("OV not available")
        result = sched._run_freshen(_backend=backend)
        assert result["checked"] == 0
        assert "error" in result


# ── _run_strengthen ──


class TestRunStrengthen:
    """Scheduler strengthen job tests.

    analyze_weak_topics() is monkeypatched so tests focus on the scheduler
    orchestration logic, not the NLP analysis (covered in test_nlp_utils.py).
    """

    def _patch_weak(self, monkeypatch, topics: list):
        # Patch at the scheduler module level — analyze_weak_topics is imported at
        # module top-level, so this is the correct and only reliable patch target.
        monkeypatch.setattr("curator.scheduler.analyze_weak_topics", lambda *a, **kw: topics)

    def test_no_weak_topics_returns_zeros(self, monkeypatch, tmp_path):
        self._patch_weak(monkeypatch, [])
        run_fn = _mock_run_fn()
        result = sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path))
        assert result == {"strengthened": 0, "skipped": 0}
        assert run_fn.calls == []

    def test_strengthens_top_n_topics(self, monkeypatch, tmp_path):
        topics = [
            {"topic": "Redis deployment", "avg_coverage": 0.2},
            {"topic": "Python async", "avg_coverage": 0.25},
            {"topic": "Docker networking", "avg_coverage": 0.3},
            {"topic": "Kubernetes", "avg_coverage": 0.35},
        ]
        self._patch_weak(monkeypatch, topics)
        run_fn = _mock_run_fn()
        result = sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path), top_n=2)
        assert result["strengthened"] == 2
        assert result["skipped"] == 0
        assert len(run_fn.calls) == 2
        assert "Redis deployment" in run_fn.calls[0]
        assert "Python async" in run_fn.calls[1]

    def test_query_contains_topic(self, monkeypatch, tmp_path):
        self._patch_weak(monkeypatch, [{"topic": "database indexing"}])
        run_fn = _mock_run_fn()
        sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path), top_n=1)
        assert "database indexing" in run_fn.calls[0]

    def test_run_error_does_not_abort_remaining(self, monkeypatch, tmp_path):
        self._patch_weak(monkeypatch, [{"topic": "topic_a"}, {"topic": "topic_b"}])
        call_count = [0]

        def _flaky(query, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first fails")

        result = sched._run_strengthen(_run_fn=_flaky, data_path=str(tmp_path), top_n=2)
        assert result["strengthened"] == 1
        assert result["skipped"] == 1  # skipped = pipeline execution failures

    def test_analyze_error_returns_zeros(self, monkeypatch, tmp_path):
        # If analyze_weak_topics itself raises, strengthen returns zeros gracefully
        def _raise(*a, **kw):
            raise OSError("disk error")

        monkeypatch.setattr("curator.scheduler.analyze_weak_topics", _raise)
        run_fn = _mock_run_fn()
        result = sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path))
        assert result == {"strengthened": 0, "skipped": 0}

    def test_non_dict_entries_are_skipped(self, monkeypatch, tmp_path):
        # analyze_weak_topics should never return non-dicts, but scheduler is defensive.
        # Non-dict entries are silently filtered — they do NOT count as skipped (pipeline failures).
        self._patch_weak(monkeypatch, ["plain string", {"topic": "valid topic"}, 42])
        run_fn = _mock_run_fn()
        result = sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path), top_n=3)
        assert result["strengthened"] == 1
        assert result["skipped"] == 0  # non-dict entries are filtered, not "failed"
        assert "valid topic" in run_fn.calls[0]

    def test_invalid_top_n_env_falls_back_to_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CURATOR_STRENGTHEN_TOP_N", "not-a-number")
        self._patch_weak(monkeypatch, [{"topic": "topic_a"}, {"topic": "topic_b"}])
        run_fn = _mock_run_fn()
        result = sched._run_strengthen(_run_fn=run_fn, data_path=str(tmp_path))
        # Falls back to top_n=3, processes all 2 topics
        assert result["strengthened"] == 2


# ── Lifecycle ──


class TestSchedulerLifecycle:
    def setup_method(self):
        sched.stop_scheduler()  # ensure clean state

    def teardown_method(self):
        sched.stop_scheduler()

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("CURATOR_SCHEDULER_ENABLED", raising=False)
        result = sched.start_scheduler()
        assert result is False
        assert sched._scheduler is None

    def test_disabled_when_set_to_zero(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "0")
        result = sched.start_scheduler()
        assert result is False
        assert sched._scheduler is None

    def test_start_when_enabled(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = []

        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            result = sched.start_scheduler()

        assert result is True
        mock_sched.start.assert_called_once()
        assert mock_sched.add_job.call_count == 2

    def test_idempotent_start(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = []

        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            r1 = sched.start_scheduler()
            r2 = sched.start_scheduler()  # second call is no-op

        assert r1 is True
        assert r2 is False
        mock_sched.start.assert_called_once()

    def test_stop_clears_scheduler(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = []

        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            sched.start_scheduler()

        assert sched._scheduler is not None
        sched.stop_scheduler()
        assert sched._scheduler is None
        mock_sched.shutdown.assert_called_once_with(wait=False)

    def test_jobs_registered_with_correct_ids(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = []

        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            sched.start_scheduler()

        call_kwargs = [call.kwargs for call in mock_sched.add_job.call_args_list]
        ids = {kw["id"] for kw in call_kwargs}
        assert ids == {"freshness_scan", "strengthen"}


# ── scheduler_status ──


class TestSchedulerStatus:
    def setup_method(self):
        sched.stop_scheduler()

    def teardown_method(self):
        sched.stop_scheduler()

    def test_status_when_not_running(self):
        status = sched.scheduler_status()
        assert status == {"running": False, "jobs": []}

    def test_status_when_running(self, monkeypatch):
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        import datetime

        mock_job = MagicMock()
        mock_job.id = "freshness_scan"
        mock_job.next_run_time = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)

        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = [mock_job]

        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            sched.start_scheduler()

        status = sched.scheduler_status()
        assert status["running"] is True
        assert len(status["jobs"]) == 1
        assert status["jobs"][0]["id"] == "freshness_scan"
        assert "2026-01-01" in status["jobs"][0]["next_run"]


class TestSchedulerIntervalValidation:
    """Verify scheduler interval clamping for edge-case values."""

    def test_zero_interval_clamped_to_one(self, monkeypatch):
        """CURATOR_FRESHNESS_INTERVAL_HOURS=0 should be clamped to 1h, not crash."""
        monkeypatch.setattr(sched, "_scheduler", None)
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        monkeypatch.setenv("CURATOR_FRESHNESS_INTERVAL_HOURS", "0")
        monkeypatch.setenv("CURATOR_STRENGTHEN_INTERVAL_HOURS", "-5")

        mock_sched = MagicMock()
        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            result = sched.start_scheduler()

        assert result is True
        # Both add_job calls should use clamped hours (>= 1.0)
        for call in mock_sched.add_job.call_args_list:
            assert call.kwargs.get("hours", call[1].get("hours", 1)) >= 1.0
        sched.stop_scheduler()

    def test_non_numeric_interval_raises(self, monkeypatch):
        """Non-numeric interval should cause start_scheduler to return False (caught)."""
        monkeypatch.setattr(sched, "_scheduler", None)
        monkeypatch.setenv("CURATOR_SCHEDULER_ENABLED", "1")
        monkeypatch.setenv("CURATOR_FRESHNESS_INTERVAL_HOURS", "not-a-number")

        mock_sched = MagicMock()
        with patch("apscheduler.schedulers.background.BackgroundScheduler", return_value=mock_sched):
            result = sched.start_scheduler()

        # Should fail gracefully (ValueError caught in the try block)
        assert result is False
        sched.stop_scheduler()
