"""Tests for curator.governance_flags — flag CRUD, filtering, expiry, batch."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from curator.governance_flags import (
    batch_update_flags,
    create_flag,
    expire_flags,
    load_flags,
    update_flag_status,
)


def _ts(days_ago: int = 0) -> str:
    t = time.time() - days_ago * 86400
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


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


# ── Flag filtering tests ──────────────────────────────────────────────────────


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


# ── update_flag_status with reason tests ──────────────────────────────────────


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


# ── expire_flags tests ─────────────────────────────────────────────────────────


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


# ── batch_update_flags tests ───────────────────────────────────────────────────


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
