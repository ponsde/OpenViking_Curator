"""Tests for curator.log_rotation — size-based JSONL rotation."""

import os

from curator.log_rotation import _numbered_path, maybe_rotate


class TestNumberedPath:
    def test_basic(self):
        assert _numbered_path("/data/query_log.jsonl", 1) == "/data/query_log.1.jsonl"
        assert _numbered_path("/data/query_log.jsonl", 3) == "/data/query_log.3.jsonl"

    def test_no_ext(self):
        assert _numbered_path("/data/logfile", 2) == "/data/logfile.2"


class TestMaybeRotate:
    def test_no_rotation_when_small(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text("small\n")
        assert maybe_rotate(str(p), max_mb=1.0) is False
        assert p.read_text() == "small\n"

    def test_no_rotation_when_disabled(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text("x" * 2_000_000)
        assert maybe_rotate(str(p), max_mb=0) is False

    def test_no_rotation_missing_file(self, tmp_path):
        p = tmp_path / "missing.jsonl"
        assert maybe_rotate(str(p), max_mb=1.0) is False

    def test_rotates_when_over_limit(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text("x" * 200)  # 200 bytes
        assert maybe_rotate(str(p), max_mb=0.0001, keep=3) is True
        # Original file should be gone (renamed to .1)
        assert not p.exists()
        backup1 = tmp_path / "test.1.jsonl"
        assert backup1.exists()
        assert backup1.read_text() == "x" * 200

    def test_shifts_existing_backups(self, tmp_path):
        p = tmp_path / "test.jsonl"
        b1 = tmp_path / "test.1.jsonl"

        b1.write_text("backup1")
        p.write_text("x" * 200)

        assert maybe_rotate(str(p), max_mb=0.0001, keep=3) is True

        assert not p.exists()
        assert (tmp_path / "test.1.jsonl").read_text() == "x" * 200
        assert (tmp_path / "test.2.jsonl").read_text() == "backup1"
        assert not (tmp_path / "test.4.jsonl").exists()

    def test_drops_oldest_beyond_keep(self, tmp_path):
        p = tmp_path / "test.jsonl"
        b1 = tmp_path / "test.1.jsonl"
        b2 = tmp_path / "test.2.jsonl"
        b3 = tmp_path / "test.3.jsonl"

        b1.write_text("b1")
        b2.write_text("b2")
        b3.write_text("b3-oldest")
        p.write_text("x" * 200)

        assert maybe_rotate(str(p), max_mb=0.0001, keep=3) is True

        # b3 (oldest) should be deleted, everything else shifted
        assert not p.exists()
        assert (tmp_path / "test.1.jsonl").read_text() == "x" * 200
        assert (tmp_path / "test.2.jsonl").read_text() == "b1"
        assert (tmp_path / "test.3.jsonl").read_text() == "b2"

    def test_new_file_created_after_rotation(self, tmp_path):
        """After rotation, appending to original path creates a fresh file."""
        p = tmp_path / "test.jsonl"
        p.write_text("x" * 200)
        maybe_rotate(str(p), max_mb=0.0001, keep=2)

        # Original gone, write a new one
        with open(str(p), "a") as f:
            f.write("fresh\n")
        assert p.read_text() == "fresh\n"
        assert (tmp_path / "test.1.jsonl").read_text() == "x" * 200


class TestLockedAppendIntegration:
    """Test that locked_append triggers rotation."""

    def test_rotation_on_append(self, tmp_path, monkeypatch):
        monkeypatch.setattr("curator.config.LOG_ROTATE_MB", 0.0001)
        monkeypatch.setattr("curator.config.LOG_ROTATE_KEEP", 2)

        from curator.file_lock import locked_append

        p = tmp_path / "log.jsonl"
        p.write_text("x" * 200)

        locked_append(str(p), "new line\n")

        # Original should have been rotated, new line in fresh file
        assert p.read_text() == "new line\n"
        assert (tmp_path / "log.1.jsonl").exists()
