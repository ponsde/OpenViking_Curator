"""Async ingest job tracking and recovery.

Tracks background judge+ingest jobs with state transitions:
    queued → running → success | failed

Failed transient jobs (timeout, 429, 5xx) can be replayed.
All state is append-only in DATA_PATH/async_ingest_jobs.jsonl.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from .config import DATA_PATH, log

_JOBS_FILE = "async_ingest_jobs.jsonl"

# Transient error patterns that are retryable
_TRANSIENT_PATTERNS = ("timeout", "429", "5xx", "502", "503", "504", "connection", "temporary")


def _jobs_path() -> str:
    return os.path.join(DATA_PATH, _JOBS_FILE)


def create_job(query: str, scope: dict | None = None) -> str:
    """Create a new job in 'queued' state. Returns job_id."""
    job_id = uuid.uuid4().hex[:12]
    _append_event(job_id, "queued", query=query, scope=scope or {})
    return job_id


def update_job(job_id: str, status: str, **extra) -> None:
    """Record a state transition for a job."""
    _append_event(job_id, status, **extra)


def _append_event(job_id: str, status: str, **extra) -> None:
    """Append a job event to the jobs file."""
    try:
        os.makedirs(DATA_PATH, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "status": status,
            **extra,
        }
        with open(_jobs_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("async_jobs: failed to write event: %s", e)


def load_all_events() -> list[dict]:
    """Load all job events from the jobs file."""
    path = _jobs_path()
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def get_job_states() -> dict[str, dict]:
    """Build current state for each job (latest event wins).

    Returns dict keyed by job_id with latest status, query, timestamps.
    """
    events = load_all_events()
    jobs: dict[str, dict] = {}
    for e in events:
        jid = e.get("job_id", "")
        if not jid:
            continue
        if jid not in jobs:
            jobs[jid] = {
                "job_id": jid,
                "query": e.get("query", ""),
                "scope": e.get("scope", {}),
                "status": e["status"],
                "created_at": e["timestamp"],
                "updated_at": e["timestamp"],
                "error": None,
                "retries": 0,
            }
        else:
            jobs[jid]["status"] = e["status"]
            jobs[jid]["updated_at"] = e["timestamp"]
        if e["status"] == "failed":
            jobs[jid]["error"] = e.get("error", "")
            jobs[jid]["retries"] = jobs[jid].get("retries", 0) + 1
        if e.get("query"):
            jobs[jid]["query"] = e["query"]
    return jobs


def list_failed() -> list[dict]:
    """Return all jobs whose latest state is 'failed'."""
    return [j for j in get_job_states().values() if j["status"] == "failed"]


def list_by_status(status: str) -> list[dict]:
    """Return all jobs with the given latest status."""
    return [j for j in get_job_states().values() if j["status"] == status]


def is_transient_error(error: str) -> bool:
    """Check if an error string looks like a transient/retryable failure."""
    if not error:
        return False
    lower = error.lower()
    return any(p in lower for p in _TRANSIENT_PATTERNS)


def get_retryable_jobs(max_retries: int = 3) -> list[dict]:
    """Return failed jobs that are retryable (transient error + under retry limit)."""
    failed = list_failed()
    return [j for j in failed if is_transient_error(j.get("error", "")) and j.get("retries", 0) <= max_retries]
