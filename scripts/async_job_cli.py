#!/usr/bin/env python3
"""CLI for managing async ingest jobs.

Commands:
    list              Show all jobs (latest state)
    list --failed     Show only failed jobs
    list --retryable  Show failed jobs that can be retried
    replay <job_id>   Re-queue a failed job for retry

Usage:
    python3 scripts/async_job_cli.py list
    python3 scripts/async_job_cli.py list --failed
    python3 scripts/async_job_cli.py list --retryable
    python3 scripts/async_job_cli.py replay abc123def456
    python3 scripts/async_job_cli.py replay --all-retryable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator.async_jobs import (
    create_job,
    get_job_states,
    get_retryable_jobs,
    list_by_status,
    list_failed,
    update_job,
)


def cmd_list(args: argparse.Namespace) -> None:
    """List jobs filtered by status."""
    if args.retryable:
        jobs = get_retryable_jobs(max_retries=args.max_retries)
        label = "retryable"
    elif args.failed:
        jobs = list_failed()
        label = "failed"
    elif args.status:
        jobs = list_by_status(args.status)
        label = args.status
    else:
        jobs = list(get_job_states().values())
        label = "all"

    if args.json:
        print(json.dumps(jobs, indent=2, ensure_ascii=False))
        return

    if not jobs:
        print(f"No {label} jobs found.")
        return

    print(f"{'JOB ID':<14} {'STATUS':<10} {'RETRIES':<8} {'QUERY':<40} {'ERROR'}")
    print("-" * 100)
    for j in jobs:
        query = (j.get("query") or "")[:40]
        error = (j.get("error") or "")[:40]
        print(f"{j['job_id']:<14} {j['status']:<10} {j.get('retries', 0):<8} {query:<40} {error}")
    print(f"\nTotal: {len(jobs)} {label} job(s)")


def cmd_replay(args: argparse.Namespace) -> None:
    """Re-queue a failed job for retry."""
    if args.all_retryable:
        retryable = get_retryable_jobs(max_retries=args.max_retries)
        if not retryable:
            print("No retryable jobs found.")
            return
        for j in retryable:
            update_job(j["job_id"], "queued")
            print(f"Re-queued: {j['job_id']} (query: {j.get('query', '')[:50]})")
        print(f"\nRe-queued {len(retryable)} job(s)")
        return

    if not args.job_id:
        print("Error: provide a job_id or use --all-retryable", file=sys.stderr)
        sys.exit(1)

    states = get_job_states()
    job = states.get(args.job_id)
    if not job:
        print(f"Error: job {args.job_id} not found", file=sys.stderr)
        sys.exit(1)
    if job["status"] != "failed":
        print(f"Error: job {args.job_id} is '{job['status']}', not 'failed'", file=sys.stderr)
        sys.exit(1)

    update_job(args.job_id, "queued")
    print(f"Re-queued: {args.job_id} (query: {job.get('query', '')[:50]})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage async ingest jobs")
    sub = parser.add_subparsers(dest="command")

    # list
    ls = sub.add_parser("list", help="List jobs")
    ls.add_argument("--failed", action="store_true", help="Show only failed jobs")
    ls.add_argument("--retryable", action="store_true", help="Show retryable failed jobs")
    ls.add_argument("--status", type=str, help="Filter by status (queued/running/success/failed)")
    ls.add_argument("--max-retries", type=int, default=3, help="Max retries for retryable filter")
    ls.add_argument("--json", action="store_true", help="Output as JSON")

    # replay
    rp = sub.add_parser("replay", help="Re-queue a failed job")
    rp.add_argument("job_id", nargs="?", help="Job ID to replay")
    rp.add_argument("--all-retryable", action="store_true", help="Replay all retryable jobs")
    rp.add_argument("--max-retries", type=int, default=3, help="Max retries for retryable filter")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "replay":
        cmd_replay(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
