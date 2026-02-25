"""Tests for curator.review_cli."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Set required env vars before importing curator modules
os.environ.setdefault("OAI_BASE", "http://localhost:8000/v1")
os.environ.setdefault("OAI_KEY", "test-key")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curator.backend_memory import InMemoryBackend
from curator import review_cli


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_ENTRY = {
    "time": "2026-02-25T12:00:00Z",
    "reason": "conflict:human_review",
    "query": "OpenViking session commit 怎么调用？",
    "trust": 7,
    "freshness": "current",
    "conflict_summary": "local says v1, external says v2",
    "conflict_preferred": "human_review",
    "markdown": "# OpenViking Session\n\n调用 `session_commit()` 完成会话。\n",
}

SAMPLE_ENTRY_2 = {
    "time": "2026-02-25T13:00:00Z",
    "reason": "review_mode",
    "query": "Docker 部署指南",
    "trust": 8,
    "freshness": "recent",
    "conflict_summary": "",
    "conflict_preferred": "",
    "markdown": "# Docker 部署\n\n使用 docker-compose up。\n",
}


def _write_jsonl(path: str, entries: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> list[dict]:
    return review_cli._load_entries(path)


def _make_args(**kwargs):
    """Create a mock Namespace for CLI commands."""
    defaults = {
        "file": "/tmp/test_pending.jsonl",
        "in_memory": True,
    }
    defaults.update(kwargs)
    return MagicMock(**defaults)


# ── _load_entries / _save_entries ──────────────────────────────────────────────

class TestLoadSave(unittest.TestCase):

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.jsonl")
            entries = review_cli._load_entries(path)
            self.assertEqual(entries, [])

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            entries = [dict(SAMPLE_ENTRY), dict(SAMPLE_ENTRY_2)]
            review_cli._save_entries(path, entries)
            loaded = review_cli._load_entries(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["query"], SAMPLE_ENTRY["query"])
            self.assertEqual(loaded[1]["freshness"], "recent")

    def test_skip_invalid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps(SAMPLE_ENTRY) + "\n")
                f.write("NOT_JSON\n")
                f.write(json.dumps(SAMPLE_ENTRY_2) + "\n")
            entries = review_cli._load_entries(path)
            self.assertEqual(len(entries), 2)


# ── _extract_title ─────────────────────────────────────────────────────────────

class TestExtractTitle(unittest.TestCase):

    def test_h1_extraction(self):
        md = "# My Title\n\nsome content"
        self.assertEqual(review_cli._extract_title(md), "My Title")

    def test_fallback_no_heading(self):
        md = "just plain text"
        self.assertEqual(review_cli._extract_title(md, "fallback"), "fallback")

    def test_fallback_empty(self):
        self.assertEqual(review_cli._extract_title("", "fb"), "fb")

    def test_multiline_first_h1(self):
        md = "intro\n\n# Real Title\n\n## Sub"
        self.assertEqual(review_cli._extract_title(md), "Real Title")


# ── cmd_list ──────────────────────────────────────────────────────────────────

class TestCmdList(unittest.TestCase):

    def test_list_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            args = _make_args(file=path)
            args.all = False
            rc = review_cli.cmd_list(args)
            self.assertEqual(rc, 0)

    def test_list_pending_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            entries = [
                dict(SAMPLE_ENTRY, status="pending"),
                dict(SAMPLE_ENTRY_2, status="approved"),
            ]
            _write_jsonl(path, entries)
            args = _make_args(file=path)
            args.all = False
            rc = review_cli.cmd_list(args)
            self.assertEqual(rc, 0)

    def test_list_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            entries = [
                dict(SAMPLE_ENTRY, status="approved"),
                dict(SAMPLE_ENTRY_2, status="rejected"),
            ]
            _write_jsonl(path, entries)
            args = _make_args(file=path)
            args.all = True
            rc = review_cli.cmd_list(args)
            self.assertEqual(rc, 0)

    def test_list_no_status_field_treated_as_pending(self):
        """Entries without 'status' key should appear in list (treated as pending)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])  # no status key
            args = _make_args(file=path)
            args.all = False
            rc = review_cli.cmd_list(args)
            self.assertEqual(rc, 0)


# ── cmd_show ──────────────────────────────────────────────────────────────────

class TestCmdShow(unittest.TestCase):

    def test_show_valid_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=0)
            rc = review_cli.cmd_show(args)
            self.assertEqual(rc, 0)

    def test_show_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=5)
            rc = review_cli.cmd_show(args)
            self.assertEqual(rc, 1)

    def test_show_negative_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=-1)
            rc = review_cli.cmd_show(args)
            self.assertEqual(rc, 1)


# ── cmd_approve ───────────────────────────────────────────────────────────────

class TestCmdApprove(unittest.TestCase):

    def _approve(self, path: str, index: int):
        args = _make_args(file=path, index=index, in_memory=True)
        # We patch _make_backend to return InMemoryBackend directly
        backend = InMemoryBackend()
        with patch.object(review_cli, "_make_backend", return_value=backend):
            rc = review_cli.cmd_approve(args)
        return rc, backend

    def test_approve_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            rc, backend = self._approve(path, 0)
            self.assertEqual(rc, 0)
            # Check status updated in file
            entries = _read_jsonl(path)
            self.assertEqual(entries[0]["status"], "approved")

    def test_approve_ingests_to_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            rc, backend = self._approve(path, 0)
            self.assertEqual(rc, 0)
            uris = backend.list_resources()
            self.assertTrue(len(uris) > 0, "Expected at least one URI in backend after approve")

    def test_approve_already_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY, status="approved")])
            args = _make_args(file=path, index=0, in_memory=True)
            backend = InMemoryBackend()
            with patch.object(review_cli, "_make_backend", return_value=backend):
                rc = review_cli.cmd_approve(args)
            self.assertEqual(rc, 0)
            # No new URIs should be added
            self.assertEqual(backend.list_resources(), [])

    def test_approve_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=99, in_memory=True)
            backend = InMemoryBackend()
            with patch.object(review_cli, "_make_backend", return_value=backend):
                rc = review_cli.cmd_approve(args)
            self.assertEqual(rc, 1)

    def test_approve_no_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            entry = dict(SAMPLE_ENTRY, markdown="")
            _write_jsonl(path, [entry])
            args = _make_args(file=path, index=0, in_memory=True)
            backend = InMemoryBackend()
            with patch.object(review_cli, "_make_backend", return_value=backend):
                rc = review_cli.cmd_approve(args)
            self.assertEqual(rc, 1)

    def test_approve_backend_init_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=0, in_memory=False)
            with patch.object(review_cli, "_make_backend", side_effect=RuntimeError("no OV")):
                rc = review_cli.cmd_approve(args)
            self.assertEqual(rc, 1)
            # Status should NOT be updated
            entries = _read_jsonl(path)
            self.assertNotEqual(entries[0].get("status"), "approved")

    def test_approve_uses_first_h1_as_title(self):
        """ingest_markdown_v2 should receive the H1 title from markdown."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            captured = {}
            backend = InMemoryBackend()

            original_ingest = backend.ingest
            def capturing_ingest(content, title="", metadata=None):
                captured["title"] = title
                return original_ingest(content, title=title, metadata=metadata)

            backend.ingest = capturing_ingest
            args = _make_args(file=path, index=0, in_memory=True)
            with patch.object(review_cli, "_make_backend", return_value=backend):
                review_cli.cmd_approve(args)
            self.assertIn("OpenViking Session", captured.get("title", ""))

    def test_approve_passes_source_urls_and_quality_feedback(self):
        """source_urls and quality_feedback must be forwarded to ingest_markdown_v2."""
        entry_with_urls = dict(SAMPLE_ENTRY, source_urls=["https://example.com/doc1", "https://example.com/doc2"])
        captured = {}

        def fake_ingest(backend, title, markdown, freshness="unknown",
                        source_urls=None, quality_feedback=None):
            captured["source_urls"] = source_urls
            captured["quality_feedback"] = quality_feedback
            return {"root_uri": "viking://test/1", "path": "/tmp/test.md", "status": "ok"}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [entry_with_urls])
            args = _make_args(file=path, index=0, in_memory=True)
            backend = InMemoryBackend()
            with patch.object(review_cli, "_make_backend", return_value=backend):
                with patch("curator.review.ingest_markdown_v2", side_effect=fake_ingest):
                    review_cli.cmd_approve(args)

        self.assertEqual(captured.get("source_urls"), ["https://example.com/doc1", "https://example.com/doc2"])
        qf = captured.get("quality_feedback", {})
        self.assertEqual(qf.get("approved_by"), "review_cli")
        self.assertEqual(qf.get("judge_trust"), SAMPLE_ENTRY["trust"])
        # judge_reason should use entry["reason"], not conflict_summary
        self.assertEqual(qf.get("judge_reason"), entry_with_urls.get("reason", ""))


# ── cmd_reject ────────────────────────────────────────────────────────────────

class TestCmdReject(unittest.TestCase):

    def test_reject_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=0)
            args.reason = ""
            rc = review_cli.cmd_reject(args)
            self.assertEqual(rc, 0)
            entries = _read_jsonl(path)
            self.assertEqual(entries[0]["status"], "rejected")

    def test_reject_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=0)
            args.reason = "outdated info"
            rc = review_cli.cmd_reject(args)
            self.assertEqual(rc, 0)
            entries = _read_jsonl(path)
            self.assertEqual(entries[0]["status"], "rejected")
            self.assertEqual(entries[0]["reject_reason"], "outdated info")

    def test_reject_already_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY, status="rejected")])
            args = _make_args(file=path, index=0)
            args.reason = ""
            rc = review_cli.cmd_reject(args)
            self.assertEqual(rc, 0)

    def test_reject_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            args = _make_args(file=path, index=10)
            args.reason = ""
            rc = review_cli.cmd_reject(args)
            self.assertEqual(rc, 1)

    def test_reject_preserves_other_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY), dict(SAMPLE_ENTRY_2)])
            args = _make_args(file=path, index=0)
            args.reason = ""
            review_cli.cmd_reject(args)
            entries = _read_jsonl(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["status"], "rejected")
            self.assertEqual(entries[1].get("status", "pending"), "pending")


# ── cmd_gc ────────────────────────────────────────────────────────────────────

class TestCmdGC(unittest.TestCase):

    def test_gc_removes_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            entries = [
                dict(SAMPLE_ENTRY, status="pending"),
                dict(SAMPLE_ENTRY_2, status="approved"),
                dict(SAMPLE_ENTRY, status="rejected"),
            ]
            _write_jsonl(path, entries)
            args = _make_args(file=path)
            rc = review_cli.cmd_gc(args)
            self.assertEqual(rc, 0)
            remaining = _read_jsonl(path)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].get("status", "pending"), "pending")

    def test_gc_nothing_to_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])  # no status = pending
            args = _make_args(file=path)
            rc = review_cli.cmd_gc(args)
            self.assertEqual(rc, 0)
            remaining = _read_jsonl(path)
            self.assertEqual(len(remaining), 1)

    def test_gc_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [])
            args = _make_args(file=path)
            rc = review_cli.cmd_gc(args)
            self.assertEqual(rc, 0)

    def test_gc_all_processed_results_in_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [
                dict(SAMPLE_ENTRY, status="approved"),
                dict(SAMPLE_ENTRY_2, status="rejected"),
            ])
            args = _make_args(file=path)
            review_cli.cmd_gc(args)
            remaining = _read_jsonl(path)
            self.assertEqual(remaining, [])


# ── main() / argparse integration ─────────────────────────────────────────────

class TestMainArgparse(unittest.TestCase):

    def test_main_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            rc = review_cli.main(["--file", path, "list"])
            self.assertEqual(rc, 0)

    def test_main_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            rc = review_cli.main(["--file", path, "show", "0"])
            self.assertEqual(rc, 0)

    def test_main_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            rc = review_cli.main(["--file", path, "reject", "0", "--reason", "test"])
            self.assertEqual(rc, 0)
            entries = _read_jsonl(path)
            self.assertEqual(entries[0]["status"], "rejected")

    def test_main_gc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY, status="approved")])
            rc = review_cli.main(["--file", path, "gc"])
            self.assertEqual(rc, 0)
            entries = _read_jsonl(path)
            self.assertEqual(entries, [])

    def test_main_approve_in_memory(self):
        """approve --in-memory should ingest without real OV."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pr.jsonl")
            _write_jsonl(path, [dict(SAMPLE_ENTRY)])
            backend = InMemoryBackend()
            with patch.object(review_cli, "_make_backend", return_value=backend):
                rc = review_cli.main(["--file", path, "--in-memory", "approve", "0"])
            self.assertEqual(rc, 0)
            entries = _read_jsonl(path)
            self.assertEqual(entries[0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
