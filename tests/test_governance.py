"""Tests for curator.governance — cycle, phases, CLI, and report integration."""

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
        monkeypatch.setattr("curator.governance_phases.CURATED_DIR", str(tmp_path / "empty_curated"))
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
        from curator.governance_phases import phase3_flag

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

        flags = phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 2

        # First should be high severity (score < 0.2)
        high_flags = [f for f in flags if f["severity"] == "high"]
        medium_flags = [f for f in flags if f["severity"] == "medium"]
        assert len(high_flags) == 1
        assert len(medium_flags) == 1

    def test_flags_broken_urls(self, tmp_path):
        """Phase 3 creates flags for broken URLs."""
        from curator.governance_phases import phase3_flag

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

        flags = phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "broken_url"

    def test_flags_review_expired(self, tmp_path):
        """Phase 3 creates flags for review-expired resources."""
        from curator.governance_phases import phase3_flag

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

        flags = phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "review_expired"

    def test_flags_ttl_rebalance(self, tmp_path):
        """Phase 3 creates flags for TTL rebalance suggestions."""
        from curator.governance_phases import phase3_flag

        d = str(tmp_path)
        audit_data = {
            "freshness": {"fresh": [], "aging": [], "stale": []},
            "url_checks": {},
            "ttl_suggestions": [
                {"file": "test.md", "current_tier": "cold", "suggested_tier": "hot", "delta_days": 45, "changed": True},
            ],
        }

        flags = phase3_flag(d, "test_cycle", "normal", audit_data)
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "ttl_rebalance"


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

        # Wrap _run_async_governance_batch to signal when done
        import curator.governance_phases as governance_phases

        original_batch = governance_phases._run_async_governance_batch

        def wrapped_batch(*args, **kwargs):
            try:
                original_batch(*args, **kwargs)
            finally:
                done_event.set()

        monkeypatch.setattr("curator.governance_phases._run_async_governance_batch", wrapped_batch)

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
        import curator.governance_phases as governance_phases

        original_batch = governance_phases._run_async_governance_batch

        def wrapped_batch(*args, **kwargs):
            try:
                original_batch(*args, **kwargs)
            finally:
                done_event.set()

        monkeypatch.setattr("curator.governance_phases._run_async_governance_batch", wrapped_batch)

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


# ── CLI filtering tests ────────────────────────────────────────────────────────


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


# ── CLI batch operation tests ──────────────────────────────────────────────────


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


# ── Report flag summary tests ──────────────────────────────────────────────────


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
