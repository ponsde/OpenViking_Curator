"""Tests for scripts/async_job_cli.py CLI commands."""

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestAsyncJobCLI(unittest.TestCase):
    """Test CLI list and replay commands."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = patch("curator.async_jobs.DATA_PATH", self.tmpdir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_jobs(self):
        """Create a few jobs in various states."""
        from curator.async_jobs import create_job, update_job

        j1 = create_job("successful query")
        update_job(j1, "running")
        update_job(j1, "success")

        j2 = create_job("transient fail query")
        update_job(j2, "running")
        update_job(j2, "failed", error="timeout after 60s")

        j3 = create_job("permanent fail query")
        update_job(j3, "running")
        update_job(j3, "failed", error="invalid API key")

        return j1, j2, j3

    def test_list_all(self):
        from scripts.async_job_cli import cmd_list

        self._seed_jobs()
        args = _make_args(failed=False, retryable=False, status=None, max_retries=3, json=False)
        output = _capture_stdout(lambda: cmd_list(args))
        self.assertIn("JOB ID", output)
        self.assertIn("success", output)
        self.assertIn("failed", output)
        self.assertIn("Total: 3", output)

    def test_list_failed(self):
        from scripts.async_job_cli import cmd_list

        self._seed_jobs()
        args = _make_args(failed=True, retryable=False, status=None, max_retries=3, json=False)
        output = _capture_stdout(lambda: cmd_list(args))
        self.assertIn("Total: 2 failed", output)

    def test_list_retryable(self):
        from scripts.async_job_cli import cmd_list

        self._seed_jobs()
        args = _make_args(failed=False, retryable=True, status=None, max_retries=3, json=False)
        output = _capture_stdout(lambda: cmd_list(args))
        # Only the transient failure should be retryable
        self.assertIn("Total: 1 retryable", output)
        self.assertIn("transient fail query", output)

    def test_list_by_status(self):
        from scripts.async_job_cli import cmd_list

        self._seed_jobs()
        args = _make_args(failed=False, retryable=False, status="success", max_retries=3, json=False)
        output = _capture_stdout(lambda: cmd_list(args))
        self.assertIn("Total: 1 success", output)

    def test_list_json(self):
        from scripts.async_job_cli import cmd_list

        self._seed_jobs()
        args = _make_args(failed=True, retryable=False, status=None, max_retries=3, json=True)
        output = _capture_stdout(lambda: cmd_list(args))
        data = json.loads(output)
        self.assertEqual(len(data), 2)

    def test_list_empty(self):
        from scripts.async_job_cli import cmd_list

        args = _make_args(failed=False, retryable=False, status=None, max_retries=3, json=False)
        output = _capture_stdout(lambda: cmd_list(args))
        self.assertIn("No all jobs found", output)

    def test_replay_single(self):
        from scripts.async_job_cli import cmd_replay

        _, j2, _ = self._seed_jobs()
        args = _make_args(job_id=j2, all_retryable=False, max_retries=3)
        output = _capture_stdout(lambda: cmd_replay(args))
        self.assertIn("Re-queued", output)
        self.assertIn(j2, output)

        from curator.async_jobs import get_job_states

        self.assertEqual(get_job_states()[j2]["status"], "queued")

    def test_replay_not_found(self):
        from scripts.async_job_cli import cmd_replay

        self._seed_jobs()
        args = _make_args(job_id="nonexistent", all_retryable=False, max_retries=3)
        with self.assertRaises(SystemExit):
            cmd_replay(args)

    def test_replay_not_failed(self):
        from scripts.async_job_cli import cmd_replay

        j1, _, _ = self._seed_jobs()
        args = _make_args(job_id=j1, all_retryable=False, max_retries=3)
        with self.assertRaises(SystemExit):
            cmd_replay(args)

    def test_replay_all_retryable(self):
        from scripts.async_job_cli import cmd_replay

        _, j2, _ = self._seed_jobs()
        args = _make_args(job_id=None, all_retryable=True, max_retries=3)
        output = _capture_stdout(lambda: cmd_replay(args))
        self.assertIn("Re-queued", output)
        self.assertIn("1 job(s)", output)

        from curator.async_jobs import get_job_states

        self.assertEqual(get_job_states()[j2]["status"], "queued")


def _make_args(**kwargs):
    """Create a simple namespace for argparse args."""
    from argparse import Namespace

    return Namespace(**kwargs)


def _capture_stdout(fn):
    """Capture stdout from a function call."""
    buf = StringIO()
    with patch("sys.stdout", buf):
        fn()
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
