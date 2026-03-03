"""Pending Review CLI — inspect / approve / reject pending_review.jsonl entries.

Usage::

    python3 -m curator.review_cli list
    python3 -m curator.review_cli show <index>
    python3 -m curator.review_cli approve <index>
    python3 -m curator.review_cli reject <index> [--reason "..."]
    python3 -m curator.review_cli gc

The pending_review.jsonl path defaults to DATA_PATH/pending_review.jsonl
(env var CURATOR_DATA_PATH, or ./data/).

Each JSONL line format::

    {
      "time": "2026-02-25T12:00:00Z",
      "reason": "conflict:human_review",
      "query": "...",
      "trust": 7,
      "freshness": "current",
      "conflict_summary": "...",
      "conflict_preferred": "human_review",
      "markdown": "# 知识标题\\n...",
      "status": "pending" | "approved" | "rejected"   # added by this CLI
    }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .config import DATA_PATH

# Default path; can be overridden by --file CLI flag.
DEFAULT_PENDING_PATH = os.path.join(DATA_PATH, "pending_review.jsonl")


# ── I/O helpers ──────────────────────────────────────────────────────────────


def _load_entries(path: str) -> list[dict]:
    """Load all JSONL entries from *path*.

    Missing file is treated as empty (not an error).
    """
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    with p.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[warn] line {lineno} is not valid JSON, skipping: {e}", file=sys.stderr)
    return entries


def _save_entries(path: str, entries: list[dict]) -> None:
    """Overwrite *path* with all entries (one JSON per line)."""
    from .file_lock import locked_write

    content = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    locked_write(path, content)


def _update_entry(path: str, index: int, update: dict) -> bool:
    """Apply *update* dict to the entry at *index*, then save.

    Returns True on success, False if index is out of range.
    """
    entries = _load_entries(path)
    if index < 0 or index >= len(entries):
        return False
    entries[index].update(update)
    _save_entries(path, entries)
    return True


def _pending_entries(entries: list[dict]) -> list[tuple[int, dict]]:
    """Return (original_index, entry) pairs whose status is pending/absent."""
    return [(i, e) for i, e in enumerate(entries) if e.get("status", "pending") == "pending"]


def _extract_title(markdown: str, fallback: str = "untitled") -> str:
    """Extract first H1 heading from markdown, or use fallback."""
    if markdown:
        m = re.search(r"^#\s+(.+)", markdown, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return fallback[:80]


# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    """List all entries (pending unless --all is given)."""
    entries = _load_entries(args.file)
    if not entries:
        print("(no entries in pending_review.jsonl)")
        return 0

    show_all = getattr(args, "all", False)

    header = f"{'#':>4}  {'time':20}  {'trust':>5}  {'fresh':8}  {'reason':25}  {'status':10}  query"
    print(header)
    print("-" * len(header))

    shown = 0
    for i, entry in enumerate(entries):
        status = entry.get("status", "pending")
        if not show_all and status != "pending":
            continue
        query = entry.get("query", "")
        query_short = query[:60].replace("\n", " ")
        time_str = entry.get("time", "")[:19]  # strip trailing Z fraction
        trust = entry.get("trust", 0)
        freshness = entry.get("freshness", "unknown")[:8]
        reason = entry.get("reason", "")[:25]
        print(f"{i:>4}  {time_str:20}  {trust:>5}  {freshness:8}  {reason:25}  {status:10}  {query_short}")
        shown += 1

    if shown == 0:
        print("(no pending entries; run with --all to see all)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Show full details of one entry."""
    entries = _load_entries(args.file)
    idx = args.index
    if idx < 0 or idx >= len(entries):
        print(f"[error] index {idx} out of range (0–{len(entries)-1})", file=sys.stderr)
        return 1
    entry = entries[idx]

    print(f"=== Entry #{idx} ===")
    print(f"time:             {entry.get('time', '')}")
    print(f"status:           {entry.get('status', 'pending')}")
    print(f"reason:           {entry.get('reason', '')}")
    print(f"trust:            {entry.get('trust', 0)}/10")
    print(f"freshness:        {entry.get('freshness', 'unknown')}")
    print(f"query:            {entry.get('query', '')}")
    if entry.get("conflict_summary"):
        print(f"conflict_summary: {entry.get('conflict_summary')}")
    if entry.get("conflict_preferred"):
        print(f"conflict_preferred: {entry.get('conflict_preferred')}")
    print("\n--- markdown ---")
    print(entry.get("markdown", "(no markdown)"))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve entry: ingest into knowledge backend, mark status=approved."""
    entries = _load_entries(args.file)
    idx = args.index
    if idx < 0 or idx >= len(entries):
        print(f"[error] index {idx} out of range (0–{len(entries)-1})", file=sys.stderr)
        return 1

    entry = entries[idx]
    current_status = entry.get("status", "pending")
    if current_status == "approved":
        print(f"[info] entry #{idx} is already approved, nothing to do.")
        return 0
    if current_status == "rejected":
        print(f"[warn] entry #{idx} was rejected. Re-approving anyway.")

    markdown = entry.get("markdown", "")
    if not markdown.strip():
        print(f"[error] entry #{idx} has no markdown content, cannot approve.", file=sys.stderr)
        return 1

    title = _extract_title(markdown, fallback=entry.get("query", "untitled"))
    freshness = entry.get("freshness", "unknown")

    print(f"Approving entry #{idx}: {title[:60]!r}  (freshness={freshness})")

    # Initialise backend
    try:
        backend = _make_backend(args)
    except Exception as e:
        print(f"[error] failed to initialise backend: {e}", file=sys.stderr)
        return 1

    from .review import ingest_markdown_v2

    try:
        result = ingest_markdown_v2(
            backend,
            title=title,
            markdown=markdown,
            freshness=freshness,
            source_urls=entry.get("source_urls") or [],
            quality_feedback={
                "judge_trust": entry.get("trust", 0),
                "judge_reason": entry.get("reason", ""),
                "has_conflict": bool(entry.get("conflict_summary")),
                "conflict_summary": entry.get("conflict_summary", ""),
                "approved_by": "review_cli",
            },
        )
    except Exception as e:
        print(f"[error] ingest_markdown_v2 failed: {e}", file=sys.stderr)
        return 1

    if result.get("status") == "local_backup_only":
        print(f"[warn] backend ingest failed (local backup written to {result.get('path')})")
    else:
        print(f"Ingested → {result.get('root_uri', '?')}  backup={result.get('path', '?')}")

    # Mark as approved in JSONL
    entries[idx]["status"] = "approved"
    _save_entries(args.file, entries)
    print(f"Marked entry #{idx} as approved in {args.file}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Reject entry: mark status=rejected (keep markdown for audit)."""
    entries = _load_entries(args.file)
    idx = args.index
    if idx < 0 or idx >= len(entries):
        print(f"[error] index {idx} out of range (0–{len(entries)-1})", file=sys.stderr)
        return 1

    entry = entries[idx]
    current_status = entry.get("status", "pending")
    if current_status == "rejected":
        print(f"[info] entry #{idx} is already rejected, nothing to do.")
        return 0

    update = {"status": "rejected"}
    reason = getattr(args, "reason", None)
    if reason:
        update["reject_reason"] = reason

    entries[idx].update(update)
    _save_entries(args.file, entries)
    msg = f"Rejected entry #{idx}"
    if reason:
        msg += f" (reason: {reason})"
    print(msg)
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """GC: remove all approved/rejected entries from the JSONL file."""
    entries = _load_entries(args.file)
    before = len(entries)
    kept = [e for e in entries if e.get("status", "pending") == "pending"]
    removed = before - len(kept)

    if removed == 0:
        print("Nothing to clean up.")
        return 0

    _save_entries(args.file, kept)
    print(f"GC: removed {removed} processed entries, {len(kept)} pending remain.")
    return 0


# ── Backend factory ───────────────────────────────────────────────────────────


def _make_backend(args: argparse.Namespace):
    """Create a KnowledgeBackend from CLI args.

    Supports --in-memory flag for tests; otherwise uses OpenVikingBackend
    with the OV config file.
    """
    if getattr(args, "in_memory", False):
        from .backend_memory import InMemoryBackend

        return InMemoryBackend()

    from .backend_ov import OpenVikingBackend

    return OpenVikingBackend()


# ── CLI parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m curator.review_cli",
        description="Inspect and process pending_review.jsonl entries.",
    )
    parser.add_argument(
        "--file",
        default=DEFAULT_PENDING_PATH,
        help=f"Path to pending_review.jsonl (default: {DEFAULT_PENDING_PATH})",
    )
    parser.add_argument(
        "--in-memory",
        action="store_true",
        help="Use InMemoryBackend instead of OpenViking (for testing / dry-run).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List pending entries.")
    p_list.add_argument("--all", action="store_true", help="Show all entries including approved/rejected.")

    # show
    p_show = sub.add_parser("show", help="Show full content of one entry.")
    p_show.add_argument("index", type=int, help="0-based entry index.")

    # approve
    p_approve = sub.add_parser("approve", help="Approve and ingest one entry.")
    p_approve.add_argument("index", type=int, help="0-based entry index.")

    # reject
    p_reject = sub.add_parser("reject", help="Reject one entry.")
    p_reject.add_argument("index", type=int, help="0-based entry index.")
    p_reject.add_argument("--reason", default="", help="Optional rejection reason.")

    # gc
    sub.add_parser("gc", help="Remove all approved/rejected entries.")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "gc": cmd_gc,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
