"""Tests for curator.governance_audit — audit log write/load."""

from __future__ import annotations

from curator.governance_audit import load_audit_log, write_audit


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
