"""Tests for async_jobs job tracking and recovery."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestAsyncJobs(unittest.TestCase):
    """Test job state tracking, listing, and retry logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = patch("curator.async_jobs.DATA_PATH", self.tmpdir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_job(self):
        from curator.async_jobs import create_job, load_all_events

        job_id = create_job("test query", scope={"domain": "tech"})
        self.assertEqual(len(job_id), 12)

        events = load_all_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["job_id"], job_id)
        self.assertEqual(events[0]["status"], "queued")
        self.assertEqual(events[0]["query"], "test query")

    def test_job_lifecycle(self):
        from curator.async_jobs import create_job, get_job_states, update_job

        job_id = create_job("lifecycle query")
        update_job(job_id, "running")
        update_job(job_id, "success")

        states = get_job_states()
        self.assertEqual(states[job_id]["status"], "success")
        self.assertEqual(states[job_id]["query"], "lifecycle query")

    def test_failed_job_tracked(self):
        from curator.async_jobs import create_job, list_failed, update_job

        job_id = create_job("fail query")
        update_job(job_id, "running")
        update_job(job_id, "failed", error="timeout after 60s")

        failed = list_failed()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["job_id"], job_id)
        self.assertIn("timeout", failed[0]["error"])

    def test_list_by_status(self):
        from curator.async_jobs import create_job, list_by_status, update_job

        j1 = create_job("q1")
        j2 = create_job("q2")
        update_job(j1, "running")
        update_job(j1, "success")
        update_job(j2, "running")
        update_job(j2, "failed", error="503 service unavailable")

        self.assertEqual(len(list_by_status("success")), 1)
        self.assertEqual(len(list_by_status("failed")), 1)
        self.assertEqual(len(list_by_status("queued")), 0)

    def test_is_transient_error(self):
        from curator.async_jobs import is_transient_error

        self.assertTrue(is_transient_error("timeout after 60s"))
        self.assertTrue(is_transient_error("HTTP 429 rate limited"))
        self.assertTrue(is_transient_error("503 service unavailable"))
        self.assertTrue(is_transient_error("connection refused"))
        self.assertFalse(is_transient_error("invalid API key"))
        self.assertFalse(is_transient_error("malformed JSON response"))
        self.assertFalse(is_transient_error(""))

    def test_retryable_jobs(self):
        from curator.async_jobs import create_job, get_retryable_jobs, update_job

        # Transient failure → retryable
        j1 = create_job("q1")
        update_job(j1, "failed", error="timeout after 60s")

        # Non-transient failure → not retryable
        j2 = create_job("q2")
        update_job(j2, "failed", error="invalid API key")

        retryable = get_retryable_jobs()
        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0]["job_id"], j1)

    def test_retry_limit(self):
        from curator.async_jobs import create_job, get_retryable_jobs, update_job

        job_id = create_job("retry query")
        # Fail 4 times (over default max_retries=3)
        for _ in range(4):
            update_job(job_id, "failed", error="timeout")

        retryable = get_retryable_jobs(max_retries=3)
        self.assertEqual(len(retryable), 0)

    def test_empty_jobs_file(self):
        from curator.async_jobs import get_job_states, list_failed, load_all_events

        self.assertEqual(load_all_events(), [])
        self.assertEqual(get_job_states(), {})
        self.assertEqual(list_failed(), [])

    def test_multiple_jobs_independent(self):
        from curator.async_jobs import create_job, get_job_states, update_job

        j1 = create_job("q1")
        j2 = create_job("q2")
        update_job(j1, "success")
        update_job(j2, "failed", error="502 bad gateway")

        states = get_job_states()
        self.assertEqual(states[j1]["status"], "success")
        self.assertEqual(states[j2]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
