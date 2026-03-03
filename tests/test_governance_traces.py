"""Tests for curator.governance_traces — async trace lifecycle."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from curator.governance_traces import (
    TRACE_CONSUMED,
    TRACE_DONE,
    TRACE_FAILED,
    TRACE_QUEUED,
    harvest_async_results,
    load_trace_states,
    write_trace_event,
)


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
