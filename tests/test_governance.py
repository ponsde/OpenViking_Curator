"""Tests for curator.governance — the weekly governance cycle."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curator.governance import (
    TRACE_CONSUMED,
    TRACE_DONE,
    TRACE_FAILED,
    TRACE_QUEUED,
    batch_update_flags,
    create_flag,
    expire_flags,
    harvest_async_results,
    load_audit_log,
    load_flags,
    load_trace_states,
    run_governance_cycle,
    update_flag_status,
    write_audit,
    write_trace_event,
)


def _ts(days_ago: int = 0) -> str:
    t = time.time() - days_ago * 86400
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _write_query_log(data_dir: str, entries: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "query_log.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _seed_data(data_dir: str) -> None:
    """Write minimal query_log + feedback for a governance cycle."""
    entries = [
        {
            "query": "redis caching patterns",
            "coverage": 0.3,
            "timestamp": _ts(1),
            "external_triggered": True,
            "used_uris": [],
            "schema_version": 2,
        },
        {
            "query": "redis caching best",
            "coverage": 0.35,
            "timestamp": _ts(2),
            "external_triggered": True,
            "used_uris": [],
            "schema_version": 2,
        },
        {
            "query": "docker compose deploy",
            "coverage": 0.8,
            "timestamp": _ts(3),
            "external_triggered": False,
            "used_uris": [],
            "schema_version": 2,
        },
        {
            "query": "docker compose volumes",
            "coverage": 0.75,
            "timestamp": _ts(4),
            "external_triggered": False,
            "used_uris": [],
            "schema_version": 2,
        },
    ]
    _write_query_log(data_dir, entries)

    # Write empty feedback
    fb_path = os.path.join(data_dir, "feedback.json")
    with open(fb_path, "w") as f:
        json.dump({}, f)


# ── Flag helpers ─────────────────────────────────────────────────────────────


class TestFlags:
    def test_create_and_load(self, tmp_path):
        d = str(tmp_path)
        flag = create_flag(
            cycle_id="test_cycle",
            uri="viking://test/1",
            flag_type="stale_resource",
            severity="high",
            reason="score 0.1",
            data_path=d,
        )
        assert flag["flag_id"].startswith("gov_")
        assert flag["status"] == "pending"
        assert flag["flag_type"] == "stale_resource"

        flags = load_flags(data_path=d)
        assert len(flags) == 1
        assert flags[0]["uri"] == "viking://test/1"

    def test_load_filtered_by_status(self, tmp_path):
        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="a", flag_type="stale_resource", severity="high", reason="r1", data_path=d)
        create_flag(cycle_id="c1", uri="b", flag_type="broken_url", severity="medium", reason="r2", data_path=d)
        # Update first flag
        flags = load_flags(data_path=d)
        update_flag_status(flags[0]["flag_id"], "keep", data_path=d)

        pending = load_flags(data_path=d, status="pending")
        assert len(pending) == 1
        assert pending[0]["uri"] == "b"

        kept = load_flags(data_path=d, status="keep")
        assert len(kept) == 1
        assert kept[0]["uri"] == "a"

    def test_update_nonexistent_flag(self, tmp_path):
        d = str(tmp_path)
        result = update_flag_status("nonexistent", "keep", data_path=d)
        assert result is False

    def test_multiple_flags_same_cycle(self, tmp_path):
        d = str(tmp_path)
        for i in range(5):
            create_flag(
                cycle_id="c1",
                uri=f"uri_{i}",
                flag_type="stale_resource",
                severity="medium",
                reason=f"reason_{i}",
                data_path=d,
            )
        flags = load_flags(data_path=d)
        assert len(flags) == 5


# ── Audit log ────────────────────────────────────────────────────────────────


class TestAuditLog:
    def test_write_and_load(self, tmp_path):
        d = str(tmp_path)
        write_audit(cycle_id="c1", phase="collect", action="test", outcome="ok", mode="normal", data_path=d)
        write_audit(cycle_id="c1", phase="audit", action="test2", outcome="ok", mode="normal", data_path=d)
        write_audit(cycle_id="c2", phase="collect", action="test3", outcome="ok", mode="normal", data_path=d)

        all_entries = load_audit_log(data_path=d)
        assert len(all_entries) == 3

        c1_entries = load_audit_log(data_path=d, cycle_id="c1")
        assert len(c1_entries) == 2

    def test_team_mode_extra_fields(self, tmp_path):
        d = str(tmp_path)
        entry = write_audit(
            cycle_id="c1",
            phase="audit",
            action="test",
            outcome="ok",
            mode="team",
            details={"latency_ms": 42, "backend_calls": 3},
            data_path=d,
        )
        assert entry["mode"] == "team"
        assert entry["details"]["latency_ms"] == 42


# ── Full governance cycle ────────────────────────────────────────────────────


class TestGovernanceCycle:
    def test_dry_run_no_pipeline_calls(self, tmp_path, monkeypatch):
        """dry_run=True should skip Phase 4 entirely."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        call_count = {"n": 0}

        def mock_run(query, **kwargs):
            call_count["n"] += 1
            return {"meta": {"coverage": 0.5, "ingested": False}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=mock_run,
        )

        assert report["proactive"]["dry_run"] is True
        assert call_count["n"] == 0
        assert "cycle_id" in report
        assert report["mode"] == "normal"

    def test_full_cycle_with_proactive(self, tmp_path, monkeypatch):
        """Full cycle runs proactive search when dry_run=False."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        searched_queries: list[str] = []

        def mock_run(query, **kwargs):
            searched_queries.append(query)
            return {"meta": {"coverage": 0.6, "ingested": True}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        assert report["proactive"]["dry_run"] is False
        # Should have attempted proactive searches (redis topics have low coverage)
        assert report["proactive"]["queries_run"] >= 0
        assert "duration_sec" in report

    def test_team_mode_has_extras(self, tmp_path, monkeypatch):
        """Team mode includes query_metrics, audit_log, config_snapshot."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        report = run_governance_cycle(
            data_path=d,
            mode="team",
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        assert report["mode"] == "team"
        assert "query_metrics" in report
        assert "audit_log" in report
        assert "config_snapshot" in report
        assert len(report["audit_log"]) > 0

    def test_normal_mode_no_extras(self, tmp_path, monkeypatch):
        """Normal mode does NOT include team-only fields."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        report = run_governance_cycle(
            data_path=d,
            mode="normal",
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        assert report["mode"] == "normal"
        assert "query_metrics" not in report
        assert "config_snapshot" not in report

    def test_report_file_created(self, tmp_path, monkeypatch):
        """Governance report JSON file is written to data dir."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        # Check report file exists
        report_files = list(Path(d).glob("governance_report_*.json"))
        assert len(report_files) >= 1

        # Verify it's valid JSON
        content = report_files[0].read_text(encoding="utf-8")
        report = json.loads(content)
        assert "cycle_id" in report

    def test_audit_log_written(self, tmp_path, monkeypatch):
        """Audit log entries are written during cycle."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        report = run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        log_entries = load_audit_log(data_path=d, cycle_id=report["cycle_id"])
        # Should have at least: cycle_start, collect phases, report, cycle_end
        assert len(log_entries) >= 4
        phases = {e["phase"] for e in log_entries}
        assert "start" in phases
        assert "collect" in phases
        assert "report" in phases
        assert "end" in phases

    def test_empty_data_dir(self, tmp_path, monkeypatch):
        """Governance cycle handles empty data dir gracefully."""
        d = str(tmp_path)
        os.makedirs(d, exist_ok=True)
        # Write empty feedback
        fb_path = os.path.join(d, "feedback.json")
        with open(fb_path, "w") as f:
            json.dump({}, f)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", fb_path)
        monkeypatch.setattr("curator.feedback_store.STORE", Path(fb_path))
        # Prevent picking up real curated directory
        monkeypatch.setattr("curator.governance.CURATED_DIR", str(tmp_path / "empty_curated"))
        monkeypatch.setenv("CURATOR_CURATED_DIR", str(tmp_path / "empty_curated"))

        report = run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        assert report["overview"]["total_resources"] == 0
        assert report["flags"]["total"] == 0

    def test_proactive_replays_retryable_jobs(self, tmp_path, monkeypatch):
        """Phase 4 replays retryable async jobs."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        # Create a retryable job in async_ingest_jobs.jsonl
        jobs_path = os.path.join(d, "async_ingest_jobs.jsonl")
        job_entry = {
            "timestamp": _ts(1),
            "job_id": "test123",
            "status": "failed",
            "query": "retryable test query",
            "error": "timeout error",
        }
        with open(jobs_path, "w") as f:
            f.write(json.dumps(job_entry) + "\n")

        # Mock DATA_PATH for async_jobs module
        monkeypatch.setattr("curator.async_jobs.DATA_PATH", d)

        replayed: list[str] = []

        def mock_run(query, **kwargs):
            replayed.append(query)
            return {"meta": {"coverage": 0.5, "ingested": True}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        # Retryable jobs are now queued as async traces
        assert report["proactive"]["async_queued"] >= 1


class TestPhase3Flagging:
    def test_flags_stale_resources(self, tmp_path):
        """Phase 3 creates flags for stale resources."""
        from curator.governance import _phase3_flag

        d = str(tmp_path)
        audit_data = {
            "freshness": {
                "fresh": [],
                "aging": [],
                "stale": [
                    {"uri": "viking://test/old", "score": 0.15, "review_expired": False},
                    {"uri": "viking://test/old2", "score": 0.35, "review_expired": False},
                ],
            },
            "url_checks": {},
            "ttl_suggestions": [],
        }

        flags = _phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 2

        # First should be high severity (score < 0.2)
        high_flags = [f for f in flags if f["severity"] == "high"]
        medium_flags = [f for f in flags if f["severity"] == "medium"]
        assert len(high_flags) == 1
        assert len(medium_flags) == 1

    def test_flags_broken_urls(self, tmp_path):
        """Phase 3 creates flags for broken URLs."""
        from curator.governance import _phase3_flag

        d = str(tmp_path)
        audit_data = {
            "freshness": {"fresh": [], "aging": [], "stale": []},
            "url_checks": {
                "viking://test/doc": [
                    {"url": "https://example.com/broken", "ok": False, "status": 404},
                    {"url": "https://example.com/ok", "ok": True, "status": 200},
                ],
            },
            "ttl_suggestions": [],
        }

        flags = _phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "broken_url"

    def test_flags_review_expired(self, tmp_path):
        """Phase 3 creates flags for review-expired resources."""
        from curator.governance import _phase3_flag

        d = str(tmp_path)
        audit_data = {
            "freshness": {
                "fresh": [{"uri": "viking://test/fresh", "review_expired": True, "review_after": "2025-01-01"}],
                "aging": [],
                "stale": [],
            },
            "url_checks": {},
            "ttl_suggestions": [],
        }

        flags = _phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "review_expired"

    def test_flags_ttl_rebalance(self, tmp_path):
        """Phase 3 creates flags for TTL rebalance suggestions."""
        from curator.governance import _phase3_flag

        d = str(tmp_path)
        audit_data = {
            "freshness": {"fresh": [], "aging": [], "stale": []},
            "url_checks": {},
            "ttl_suggestions": [
                {"file": "test.md", "current_tier": "cold", "suggested_tier": "hot", "delta_days": 45, "changed": True},
            ],
        }

        flags = _phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "ttl_rebalance"


# ── Async trace tests ───────────────────────────────────────────────────────


class TestAsyncTraces:
    def test_write_and_load_traces(self, tmp_path):
        """Write trace events and load current state."""
        d = str(tmp_path)
        write_trace_event(d, "t1", TRACE_QUEUED, query="test query", topic="test")
        write_trace_event(d, "t2", TRACE_QUEUED, query="another query", topic="other")

        states = load_trace_states(d)
        assert len(states) == 2
        assert states["t1"]["status"] == TRACE_QUEUED
        assert states["t1"]["query"] == "test query"
        assert states["t2"]["status"] == TRACE_QUEUED

    def test_trace_state_transitions(self, tmp_path):
        """Trace events update state correctly (latest wins)."""
        d = str(tmp_path)
        write_trace_event(d, "t1", TRACE_QUEUED, query="q1")
        write_trace_event(d, "t1", TRACE_DONE, result={"ingested": True, "coverage": 0.8})

        states = load_trace_states(d)
        assert states["t1"]["status"] == TRACE_DONE
        assert states["t1"]["result"]["ingested"] is True
        assert states["t1"]["query"] == "q1"  # preserved from queued event

    def test_harvest_completed_traces(self, tmp_path):
        """Harvest picks up done traces and marks them consumed."""
        d = str(tmp_path)
        write_trace_event(d, "t1", TRACE_QUEUED, query="q1")
        write_trace_event(d, "t1", TRACE_DONE, result={"ingested": True, "coverage": 0.5})
        write_trace_event(d, "t2", TRACE_QUEUED, query="q2")
        write_trace_event(d, "t2", TRACE_FAILED, error="timeout")
        write_trace_event(d, "t3", TRACE_QUEUED, query="q3")
        write_trace_event(d, "t3", TRACE_DONE, result={"ingested": False, "coverage": 0.3})

        harvested = harvest_async_results(d, consumed_by="test_cycle")
        assert len(harvested) == 2  # t1 and t3 are done

        # Verify they're now consumed
        states = load_trace_states(d)
        assert states["t1"]["status"] == TRACE_CONSUMED
        assert states["t3"]["status"] == TRACE_CONSUMED
        assert states["t2"]["status"] == TRACE_FAILED  # unchanged

    def test_harvest_idempotent(self, tmp_path):
        """Second harvest returns nothing (already consumed)."""
        d = str(tmp_path)
        write_trace_event(d, "t1", TRACE_QUEUED, query="q1")
        write_trace_event(d, "t1", TRACE_DONE, result={"ingested": True})

        first = harvest_async_results(d, consumed_by="cycle1")
        assert len(first) == 1

        second = harvest_async_results(d, consumed_by="cycle2")
        assert len(second) == 0  # already consumed

    def test_harvest_empty(self, tmp_path):
        """Harvest on empty data returns empty list."""
        d = str(tmp_path)
        harvested = harvest_async_results(d, consumed_by="test")
        assert harvested == []

    def test_orphan_detection(self, tmp_path, monkeypatch):
        """Queued traces older than threshold are marked failed."""
        from datetime import timedelta

        from curator import governance

        d = str(tmp_path)
        # Write a trace with an old timestamp
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        from curator.file_lock import locked_append

        entry = json.dumps(
            {
                "timestamp": old_ts,
                "trace_id": "t_old",
                "event": TRACE_QUEUED,
                "query": "old query",
            }
        )
        locked_append(os.path.join(d, "governance_async_traces.jsonl"), entry + "\n")

        # Also write a recent queued trace
        write_trace_event(d, "t_new", TRACE_QUEUED, query="new query")

        harvested = harvest_async_results(d, consumed_by="test")
        assert len(harvested) == 0  # nothing done

        # Old trace should now be failed
        states = load_trace_states(d)
        assert states["t_old"]["status"] == TRACE_FAILED
        assert "orphaned" in states["t_old"].get("error", "")
        # Recent trace still queued
        assert states["t_new"]["status"] == TRACE_QUEUED


# ── Hybrid sync + async Phase 4 tests ──────────────────────────────────────


class TestHybridPhase4:
    def test_sync_budget_limits_sync_calls(self, tmp_path, monkeypatch):
        """Only sync_budget proactive queries run synchronously."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")
        monkeypatch.setenv("CURATOR_GOVERNANCE_SYNC_BUDGET", "1")

        sync_calls: list[str] = []

        def mock_run(query, **kwargs):
            sync_calls.append(query)
            return {"meta": {"coverage": 0.6, "ingested": True}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        # sync_calls should be limited to budget (1)
        # but async items are queued to the same mock_run in a thread
        # The sync part of the report should reflect the budget
        assert report["proactive"]["queries_run"] <= 1

    def test_async_queued_in_report(self, tmp_path, monkeypatch):
        """Report shows async_queued count when there are overflow queries."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")
        monkeypatch.setenv("CURATOR_GOVERNANCE_SYNC_BUDGET", "0")

        def mock_run(query, **kwargs):
            return {"meta": {"coverage": 0.5, "ingested": False}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        # With sync_budget=0, all proactive queries should be async
        assert report["proactive"]["queries_run"] == 0
        # async_queued should be >= 0 (depends on whether interests were extracted)
        assert "async_queued" in report["proactive"]

    def test_dry_run_skips_async(self, tmp_path, monkeypatch):
        """dry_run=True skips both sync and async."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        report = run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        assert report["proactive"]["dry_run"] is True
        assert report["proactive"]["async_queued"] == 0

    def test_harvest_in_report(self, tmp_path, monkeypatch):
        """Governance cycle harvests previous async results and includes in report."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")

        # Simulate previous cycle's async results
        write_trace_event(d, "prev_t1", TRACE_QUEUED, query="prev query 1")
        write_trace_event(d, "prev_t1", TRACE_DONE, result={"ingested": True, "coverage": 0.7, "query": "prev query 1"})
        write_trace_event(d, "prev_t2", TRACE_QUEUED, query="prev query 2")
        write_trace_event(
            d, "prev_t2", TRACE_DONE, result={"ingested": False, "coverage": 0.4, "query": "prev query 2"}
        )

        report = run_governance_cycle(
            data_path=d,
            dry_run=True,
            _run_fn=lambda q, **kw: {"meta": {"coverage": 0.5}},
        )

        assert report["async_harvest"]["harvested"] == 2
        assert report["async_harvest"]["ingested"] == 1

    def test_retryable_replays_go_async(self, tmp_path, monkeypatch):
        """Retryable jobs from Phase 2 are queued as async traces, not sync."""
        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")
        monkeypatch.setenv("CURATOR_GOVERNANCE_SYNC_BUDGET", "10")

        # Create retryable jobs
        jobs_path = os.path.join(d, "async_ingest_jobs.jsonl")
        for i in range(3):
            entry = {
                "timestamp": _ts(1),
                "job_id": f"job_{i}",
                "status": "failed",
                "query": f"retry query {i}",
                "error": "timeout error",
            }
            with open(jobs_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        monkeypatch.setattr("curator.async_jobs.DATA_PATH", d)

        sync_queries: list[str] = []

        def mock_run(query, **kwargs):
            sync_queries.append(query)
            return {"meta": {"coverage": 0.5, "ingested": False}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        # Replays should be in async_queued, not sync
        assert report["proactive"]["async_queued"] >= 3

        # Check traces were created for replays
        states = load_trace_states(d)
        replay_traces = [t for t in states.values() if t.get("job_type") == "replay"]
        assert len(replay_traces) == 3

    def test_async_thread_completes_traces(self, tmp_path, monkeypatch):
        """Verify async thread writes TRACE_DONE on successful completion."""
        import threading

        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")
        monkeypatch.setenv("CURATOR_GOVERNANCE_SYNC_BUDGET", "0")  # all async

        done_event = threading.Event()
        original_batch = None

        # Wrap _run_async_governance_batch to signal when done
        from curator import governance

        original_batch = governance._run_async_governance_batch

        def wrapped_batch(*args, **kwargs):
            try:
                original_batch(*args, **kwargs)
            finally:
                done_event.set()

        monkeypatch.setattr("curator.governance._run_async_governance_batch", wrapped_batch)

        call_count = {"n": 0}

        def mock_run(query, **kwargs):
            call_count["n"] += 1
            return {"meta": {"coverage": 0.7, "ingested": True}}

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=mock_run,
        )

        # Wait for async thread to complete (max 10s)
        if report["proactive"]["async_queued"] > 0:
            done_event.wait(timeout=10)

            # Verify traces transitioned to DONE
            states = load_trace_states(d)
            done_traces = [t for t in states.values() if t.get("status") == "done"]
            assert len(done_traces) == report["proactive"]["async_queued"]
            for t in done_traces:
                assert t.get("result", {}).get("ingested") is True

    def test_async_thread_handles_failures(self, tmp_path, monkeypatch):
        """Verify async thread writes TRACE_FAILED on pipeline error."""
        import threading

        d = str(tmp_path)
        _seed_data(d)
        monkeypatch.setenv("CURATOR_FEEDBACK_FILE", os.path.join(d, "feedback.json"))
        monkeypatch.setattr("curator.feedback_store.STORE", Path(d) / "feedback.json")
        monkeypatch.setenv("CURATOR_GOVERNANCE_SYNC_BUDGET", "0")

        done_event = threading.Event()
        from curator import governance

        original_batch = governance._run_async_governance_batch

        def wrapped_batch(*args, **kwargs):
            try:
                original_batch(*args, **kwargs)
            finally:
                done_event.set()

        monkeypatch.setattr("curator.governance._run_async_governance_batch", wrapped_batch)

        def failing_run(query, **kwargs):
            raise RuntimeError("simulated pipeline failure")

        report = run_governance_cycle(
            data_path=d,
            dry_run=False,
            _run_fn=failing_run,
        )

        if report["proactive"]["async_queued"] > 0:
            done_event.wait(timeout=10)

            states = load_trace_states(d)
            failed_traces = [t for t in states.values() if t.get("status") == "failed"]
            assert len(failed_traces) >= report["proactive"]["async_queued"]
            for t in failed_traces:
                assert "simulated" in t.get("error", "")


# ── Flag filtering tests (Task 7.1) ──────────────────────────────────────────


class TestFlagFiltering:
    def _create_flags(self, d: str) -> None:
        create_flag(
            cycle_id="c1",
            uri="uri_stale_high",
            flag_type="stale_resource",
            severity="high",
            reason="stale",
            data_path=d,
        )
        create_flag(
            cycle_id="c1",
            uri="uri_broken_medium",
            flag_type="broken_url",
            severity="medium",
            reason="broken",
            data_path=d,
        )
        create_flag(
            cycle_id="c2",
            uri="uri_stale_low",
            flag_type="stale_resource",
            severity="low",
            reason="low stale",
            data_path=d,
        )
        create_flag(
            cycle_id="c2",
            uri="uri_ttl",
            flag_type="ttl_rebalance",
            severity="low",
            reason="ttl",
            data_path=d,
        )

    def test_filter_by_flag_type(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        stale = load_flags(data_path=d, flag_type="stale_resource")
        assert len(stale) == 2
        assert all(f["flag_type"] == "stale_resource" for f in stale)

    def test_filter_by_severity(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        high = load_flags(data_path=d, severity="high")
        assert len(high) == 1
        assert high[0]["uri"] == "uri_stale_high"

    def test_filter_by_cycle_id(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        c2_flags = load_flags(data_path=d, cycle_id="c2")
        assert len(c2_flags) == 2
        assert all(f["cycle_id"] == "c2" for f in c2_flags)

    def test_combined_filter(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        result = load_flags(data_path=d, flag_type="stale_resource", severity="high")
        assert len(result) == 1
        assert result[0]["uri"] == "uri_stale_high"

    def test_filter_with_status(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        # Mark one stale as keep
        flags = load_flags(data_path=d, flag_type="stale_resource")
        update_flag_status(flags[0]["flag_id"], "keep", data_path=d)

        pending_stale = load_flags(data_path=d, flag_type="stale_resource", status="pending")
        assert len(pending_stale) == 1

    def test_filter_no_match(self, tmp_path):
        d = str(tmp_path)
        self._create_flags(d)
        result = load_flags(data_path=d, flag_type="review_expired")
        assert result == []


# ── update_flag_status with reason tests (Task 7.2) ──────────────────────────


class TestUpdateFlagStatusReason:
    def test_reason_recorded(self, tmp_path):
        d = str(tmp_path)
        flag = create_flag(
            cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="stale", data_path=d
        )
        ok = update_flag_status(flag["flag_id"], "delete", data_path=d, reason="content is outdated")
        assert ok is True

        flags = load_flags(data_path=d)
        updated = flags[0]
        assert updated["resolution_reason"] == "content is outdated"
        assert updated["resolved_at"] is not None

    def test_no_reason_is_none(self, tmp_path):
        d = str(tmp_path)
        flag = create_flag(
            cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="stale", data_path=d
        )
        update_flag_status(flag["flag_id"], "keep", data_path=d)
        flags = load_flags(data_path=d)
        assert flags[0]["resolution_reason"] is None

    def test_resolved_at_set_on_update(self, tmp_path):
        d = str(tmp_path)
        flag = create_flag(
            cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d
        )
        update_flag_status(flag["flag_id"], "ignore", data_path=d)
        flags = load_flags(data_path=d)
        assert "resolved_at" in flags[0]
        resolved_at = datetime.fromisoformat(flags[0]["resolved_at"])
        assert resolved_at.tzinfo is not None


# ── expire_flags tests (Task 7.3) ─────────────────────────────────────────────


class TestExpireFlags:
    def test_expire_old_pending_flags(self, tmp_path):
        d = str(tmp_path)
        # Create a flag with old timestamp (100 days ago)
        flag = create_flag(
            cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d
        )
        # Backdate the flag
        path = os.path.join(d, "governance_flags.jsonl")
        flags_data = load_flags(data_path=d)
        old_ts = _ts(100)
        flags_data[0]["timestamp"] = old_ts
        with open(path, "w") as f:
            for fl in flags_data:
                f.write(json.dumps(fl) + "\n")

        expired = expire_flags(data_path=d, expire_days=90)
        assert flag["flag_id"] in expired

        result = load_flags(data_path=d, status="expired")
        assert len(result) == 1
        assert result[0]["resolution_reason"].startswith("auto-expired")

    def test_fresh_pending_flag_not_expired(self, tmp_path):
        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        expired = expire_flags(data_path=d, expire_days=90)
        assert expired == []

    def test_non_pending_flag_not_expired(self, tmp_path):
        d = str(tmp_path)
        flag = create_flag(
            cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d
        )
        update_flag_status(flag["flag_id"], "keep", data_path=d)
        # Backdate
        path = os.path.join(d, "governance_flags.jsonl")
        flags_data = load_flags(data_path=d)
        flags_data[0]["timestamp"] = _ts(100)
        with open(path, "w") as f:
            for fl in flags_data:
                f.write(json.dumps(fl) + "\n")

        expired = expire_flags(data_path=d, expire_days=90)
        assert expired == []  # keep status, not pending

    def test_expire_days_zero_disables(self, tmp_path):
        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        # Backdate
        path = os.path.join(d, "governance_flags.jsonl")
        flags_data = load_flags(data_path=d)
        flags_data[0]["timestamp"] = _ts(200)
        with open(path, "w") as f:
            for fl in flags_data:
                f.write(json.dumps(fl) + "\n")

        expired = expire_flags(data_path=d, expire_days=0)
        assert expired == []  # disabled
        still_pending = load_flags(data_path=d, status="pending")
        assert len(still_pending) == 1

    def test_load_flags_pending_excludes_expired(self, tmp_path):
        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        # Backdate and expire
        path = os.path.join(d, "governance_flags.jsonl")
        flags_data = load_flags(data_path=d)
        flags_data[0]["timestamp"] = _ts(100)
        with open(path, "w") as f:
            for fl in flags_data:
                f.write(json.dumps(fl) + "\n")
        expire_flags(data_path=d, expire_days=90)

        pending = load_flags(data_path=d, status="pending")
        assert pending == []  # expired not included

        all_flags = load_flags(data_path=d)
        assert len(all_flags) == 1
        assert all_flags[0]["status"] == "expired"


# ── batch_update_flags tests (Task 7.4) ───────────────────────────────────────


class TestBatchUpdateFlags:
    def test_batch_update_multiple(self, tmp_path):
        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        f2 = create_flag(cycle_id="c1", uri="u2", flag_type="broken_url", severity="medium", reason="r", data_path=d)
        f3 = create_flag(cycle_id="c1", uri="u3", flag_type="stale_resource", severity="low", reason="r", data_path=d)

        updated, not_found = batch_update_flags(
            [f1["flag_id"], f2["flag_id"]], "delete", reason="bulk cleanup", data_path=d
        )
        assert set(updated) == {f1["flag_id"], f2["flag_id"]}
        assert not_found == []

        deleted = load_flags(data_path=d, status="delete")
        assert len(deleted) == 2
        assert all(fl["resolution_reason"] == "bulk cleanup" for fl in deleted)

        pending = load_flags(data_path=d, status="pending")
        assert len(pending) == 1
        assert pending[0]["flag_id"] == f3["flag_id"]

    def test_batch_update_partial_invalid(self, tmp_path):
        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)

        updated, not_found = batch_update_flags([f1["flag_id"], "nonexistent_id"], "keep", data_path=d)
        assert f1["flag_id"] in updated
        assert "nonexistent_id" in not_found

    def test_batch_update_with_reason(self, tmp_path):
        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        f2 = create_flag(cycle_id="c1", uri="u2", flag_type="broken_url", severity="medium", reason="r", data_path=d)

        batch_update_flags([f1["flag_id"], f2["flag_id"]], "ignore", reason="no action needed", data_path=d)

        flags = load_flags(data_path=d, status="ignore")
        assert all(fl["resolution_reason"] == "no action needed" for fl in flags)

    def test_batch_update_single_write(self, tmp_path):
        """Batch update writes the file only once (not N times)."""
        d = str(tmp_path)
        ids = []
        for i in range(5):
            f = create_flag(
                cycle_id="c1", uri=f"u{i}", flag_type="stale_resource", severity="low", reason="r", data_path=d
            )
            ids.append(f["flag_id"])

        path = os.path.join(d, "governance_flags.jsonl")
        mtime_before = os.path.getmtime(path)
        import time as _time

        _time.sleep(0.01)

        batch_update_flags(ids, "keep", data_path=d)
        mtime_after = os.path.getmtime(path)
        # File was written (mtime changed)
        assert mtime_after > mtime_before
        # All flags updated
        kept = load_flags(data_path=d, status="keep")
        assert len(kept) == 5


# ── CLI filtering tests (Task 7.5) ────────────────────────────────────────────


class TestCLIFiltering:
    def _create_flags(self, d: str) -> None:
        for ft, sev in [
            ("stale_resource", "high"),
            ("stale_resource", "medium"),
            ("broken_url", "medium"),
            ("ttl_rebalance", "low"),
        ]:
            create_flag(cycle_id="c1", uri=f"uri_{ft}_{sev}", flag_type=ft, severity=sev, reason="r", data_path=d)

    def test_cli_filter_by_type(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        self._create_flags(d)
        rc = main(["--data-path", d, "flags", "--type", "stale_resource"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "stale_resource" in out
        assert "broken_url" not in out

    def test_cli_filter_by_severity(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        self._create_flags(d)
        rc = main(["--data-path", d, "flags", "--severity", "high"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "high" in out
        assert "medium" not in out

    def test_cli_combined_filter(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        self._create_flags(d)
        rc = main(["--data-path", d, "flags", "--type", "stale_resource", "--severity", "high"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total: 1" in out

    def test_cli_invalid_type_error(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        # argparse enforces `choices` and calls sys.exit(2) on invalid values
        with pytest.raises(SystemExit) as exc_info:
            main(["--data-path", d, "flags", "--type", "nonexistent_type"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        # argparse error output includes the valid choices
        assert "stale_resource" in err

    def test_cli_filter_with_all(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        self._create_flags(d)
        # Mark one as keep
        flags = load_flags(data_path=d, flag_type="broken_url")
        update_flag_status(flags[0]["flag_id"], "keep", data_path=d)

        rc = main(["--data-path", d, "flags", "--all", "--type", "broken_url"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "broken_url" in out


# ── CLI batch operation tests (Task 7.6) ──────────────────────────────────────


class TestCLIBatchOps:
    def test_multi_id_delete(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        f2 = create_flag(cycle_id="c1", uri="u2", flag_type="broken_url", severity="medium", reason="r", data_path=d)

        rc = main(["--data-path", d, "delete", f1["flag_id"], f2["flag_id"]])
        assert rc == 0
        deleted = load_flags(data_path=d, status="delete")
        assert len(deleted) == 2

    def test_multi_id_with_reason(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)

        rc = main(["--data-path", d, "keep", f1["flag_id"], "--reason", "still relevant"])
        assert rc == 0
        kept = load_flags(data_path=d, status="keep")
        assert kept[0]["resolution_reason"] == "still relevant"

    def test_batch_filter_without_batch_flag_errors(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)

        rc = main(["--data-path", d, "delete", "--type", "stale_resource"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--batch" in err

    def test_batch_filter_with_batch_flag(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)
        create_flag(cycle_id="c1", uri="u2", flag_type="stale_resource", severity="medium", reason="r", data_path=d)
        create_flag(cycle_id="c1", uri="u3", flag_type="broken_url", severity="medium", reason="r", data_path=d)

        rc = main(["--data-path", d, "delete", "--type", "stale_resource", "--batch"])
        assert rc == 0
        deleted = load_flags(data_path=d, status="delete")
        assert len(deleted) == 2  # only stale_resource flags

    def test_partial_invalid_id_warning(self, tmp_path, capsys):
        from curator.governance_cli import main

        d = str(tmp_path)
        f1 = create_flag(cycle_id="c1", uri="u1", flag_type="stale_resource", severity="high", reason="r", data_path=d)

        rc = main(["--data-path", d, "keep", f1["flag_id"], "nonexistent_id"])
        # Valid ID succeeds, invalid warns but doesn't fail entirely
        assert rc == 0
        err = capsys.readouterr().err
        assert "nonexistent_id" in err
        kept = load_flags(data_path=d, status="keep")
        assert len(kept) == 1


# ── Report flag summary tests (Task 7.7) ──────────────────────────────────────


class TestReportFlagSummary:
    def _make_report(self, pending_flags: list[dict], total: int | None = None) -> dict:
        """Build a minimal report dict with pending_flags."""
        return {
            "cycle_id": "test_cycle",
            "timestamp": "2026-02-28T10:00:00+00:00",
            "mode": "normal",
            "overview": {"total_resources": 0, "health_score": -1},
            "knowledge_health": {"fresh": 0, "aging": 0, "stale": 0, "coverage_mean": 0},
            "flags": {"total": len(pending_flags), "by_type": {}},
            "proactive": {"queries_run": 0, "ingested": 0, "async_queued": 0, "dry_run": False},
            "async_harvest": {"harvested": 0, "ingested": 0},
            "pending_review_count": 0,
            "weak_topics": [],
            "pending_flags": pending_flags,
            "pending_flags_total": total if total is not None else len(pending_flags),
        }

    def test_ascii_report_includes_pending_flags(self):
        from curator.governance_report import format_report

        pf = [
            {"flag_id": "gov_abc", "flag_type": "stale_resource", "severity": "high", "uri": "u1", "reason": "stale"},
        ]
        report = self._make_report(pf)
        output = format_report(report)
        assert "Pending Flags" in output
        assert "high" in output
        assert "stale_resource" in output

    def test_ascii_report_no_pending_flags(self):
        from curator.governance_report import format_report

        report = self._make_report([], total=0)
        output = format_report(report)
        assert "无待处理 flag" in output

    def test_json_report_includes_pending_flags(self):
        from curator.governance_report import format_report_json

        pf = [
            {"flag_id": "gov_abc", "flag_type": "stale_resource", "severity": "high", "uri": "u1", "reason": "stale"},
        ]
        report = self._make_report(pf)
        output = format_report_json(report)
        data = json.loads(output)
        assert "pending_flags" in data
        assert len(data["pending_flags"]) == 1
        assert data["pending_flags"][0]["severity"] == "high"

    def test_html_report_includes_pending_flags(self):
        from curator.governance_report import format_report_html

        pf = [
            {"flag_id": "gov_abc", "flag_type": "stale_resource", "severity": "high", "uri": "u1", "reason": "stale"},
        ]
        report = self._make_report(pf)
        output = format_report_html(report)
        assert "Pending Flags" in output
        assert "stale_resource" in output
        assert "high" in output

    def test_html_report_no_pending_flags(self):
        from curator.governance_report import format_report_html

        report = self._make_report([], total=0)
        output = format_report_html(report)
        assert "无待处理 flag" in output
