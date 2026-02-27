"""Governance CLI — view reports, manage flags, trigger governance cycle.

Usage::

    python3 -m curator.governance_cli report
    python3 -m curator.governance_cli report --format json
    python3 -m curator.governance_cli report --format html > report.html

    python3 -m curator.governance_cli flags
    python3 -m curator.governance_cli flags --all
    python3 -m curator.governance_cli show <flag_id>
    python3 -m curator.governance_cli keep <flag_id>
    python3 -m curator.governance_cli delete <flag_id>
    python3 -m curator.governance_cli adjust <flag_id>
    python3 -m curator.governance_cli ignore <flag_id>

    python3 -m curator.governance_cli run
    python3 -m curator.governance_cli run --dry
    python3 -m curator.governance_cli run --mode team
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from glob import glob

from .config import DATA_PATH

# ── Subcommands ──────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> int:
    """Display the latest governance report."""
    from .governance_report import format_report, format_report_html, format_report_json

    data_dir = args.data_path or DATA_PATH
    pattern = os.path.join(data_dir, "governance_report_*.json")
    files = sorted(glob(pattern), reverse=True)

    if not files:
        print("No governance reports found. Run: python3 -m curator.governance_cli run")
        return 1

    report_path = files[0]
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    fmt = getattr(args, "format", "ascii")
    if fmt == "json":
        print(format_report_json(report))
    elif fmt == "html":
        print(format_report_html(report))
    else:
        print(format_report(report))

    return 0


def cmd_flags(args: argparse.Namespace) -> int:
    """List governance flags."""
    from .governance import load_flags

    data_dir = args.data_path or DATA_PATH
    show_all = getattr(args, "all", False)
    status_filter = None if show_all else "pending"
    flags = load_flags(data_path=data_dir, status=status_filter)

    if not flags:
        label = "flags" if show_all else "pending flags"
        print(f"(no {label} found)")
        return 0

    header = f"{'flag_id':>16}  {'type':20}  {'severity':8}  {'status':10}  uri"
    print(header)
    print("-" * len(header))

    for flag in flags:
        fid = flag.get("flag_id", "?")[-12:]
        ft = flag.get("flag_type", "?")[:20]
        sev = flag.get("severity", "?")[:8]
        status = flag.get("status", "?")[:10]
        uri = flag.get("uri", "?")[:60]
        print(f"{fid:>16}  {ft:20}  {sev:8}  {status:10}  {uri}")

    print(f"\nTotal: {len(flags)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Show detailed info for one flag."""
    from .governance import load_flags

    data_dir = args.data_path or DATA_PATH
    flags = load_flags(data_path=data_dir)
    target = [f for f in flags if f.get("flag_id", "").endswith(args.flag_id)]

    if not target:
        print(f"[error] flag not found: {args.flag_id}", file=sys.stderr)
        return 1
    if len(target) > 1:
        print(f"[error] ambiguous flag_id '{args.flag_id}' matches {len(target)} flags:", file=sys.stderr)
        for m in target:
            print(f"  {m['flag_id']}  ({m.get('flag_type', '?')})", file=sys.stderr)
        return 1

    flag = target[0]
    print(f"=== Flag {flag.get('flag_id', '?')} ===")
    print(f"type:     {flag.get('flag_type', '?')}")
    print(f"severity: {flag.get('severity', '?')}")
    print(f"status:   {flag.get('status', '?')}")
    print(f"uri:      {flag.get('uri', '?')}")
    print(f"reason:   {flag.get('reason', '?')}")
    print(f"cycle:    {flag.get('cycle_id', '?')}")
    print(f"time:     {flag.get('timestamp', '?')}")
    details = flag.get("details", {})
    if details:
        print(f"details:  {json.dumps(details, ensure_ascii=False)}")
    return 0


def _update_flag_cmd(args: argparse.Namespace, new_status: str) -> int:
    """Generic flag status updater."""
    from .governance import load_flags, update_flag_status

    data_dir = args.data_path or DATA_PATH

    # Resolve flag_id (allow partial suffix match)
    flags = load_flags(data_path=data_dir)
    matches = [f for f in flags if f.get("flag_id", "").endswith(args.flag_id)]
    if not matches:
        print(f"[error] flag not found: {args.flag_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"[error] ambiguous flag_id '{args.flag_id}' matches {len(matches)} flags:", file=sys.stderr)
        for m in matches:
            print(f"  {m['flag_id']}  ({m.get('flag_type', '?')}  {m.get('uri', '?')[:50]})", file=sys.stderr)
        print("Please provide a longer or full flag ID.", file=sys.stderr)
        return 1

    full_id = matches[0]["flag_id"]
    ok = update_flag_status(full_id, new_status, data_path=data_dir)
    if ok:
        print(f"Flag {full_id} → {new_status}")
        return 0
    print(f"[error] failed to update flag {args.flag_id}", file=sys.stderr)
    return 1


def cmd_keep(args: argparse.Namespace) -> int:
    """Mark flag as keep (resource is fine, keep it)."""
    return _update_flag_cmd(args, "keep")


def cmd_delete(args: argparse.Namespace) -> int:
    """Approve deletion of flagged resource."""
    return _update_flag_cmd(args, "delete")


def cmd_adjust(args: argparse.Namespace) -> int:
    """Mark flag as needing adjustment (e.g. TTL change)."""
    return _update_flag_cmd(args, "adjust")


def cmd_ignore(args: argparse.Namespace) -> int:
    """Ignore this flag (don't flag this URI again)."""
    return _update_flag_cmd(args, "ignore")


def cmd_run(args: argparse.Namespace) -> int:
    """Manually trigger a governance cycle."""
    from .governance import run_governance_cycle
    from .governance_report import format_report

    data_dir = args.data_path or DATA_PATH
    mode = getattr(args, "mode", "normal")
    dry_run = getattr(args, "dry", False)

    print(f"Starting governance cycle (mode={mode}, dry_run={dry_run})...")

    # Create backend for freshness scan (graceful if OV unavailable)
    backend = None
    try:
        from .backend_ov import OpenVikingBackend

        backend = OpenVikingBackend()
    except Exception:
        print("(warning: backend unavailable, freshness scan will be skipped)")

    try:
        report = run_governance_cycle(
            backend=backend,
            data_path=data_dir,
            mode=mode,
            dry_run=dry_run,
        )
    except Exception as e:
        print(f"[error] governance cycle failed: {e}", file=sys.stderr)
        return 1

    print(format_report(report))

    # Wait for async thread to complete (CLI is a one-shot process,
    # daemon thread would die on exit otherwise)
    async_thread = report.pop("_async_thread", None)
    if async_thread is not None and async_thread.is_alive():
        n = report.get("proactive", {}).get("async_queued", 0)
        timeout = max(120, n * 60)  # 60s per task, minimum 2 min
        print(f"\nWaiting for {n} async tasks to complete (timeout {timeout}s)...")
        async_thread.join(timeout=timeout)
        if async_thread.is_alive():
            print("Warning: async tasks did not finish within timeout, exiting.")
        else:
            print("Async tasks completed.")

    return 0


# ── CLI parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m curator.governance_cli",
        description="Curator governance: view reports, manage flags, run cycle.",
    )
    parser.add_argument(
        "--data-path",
        default=None,
        help=f"Data directory (default: {DATA_PATH})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # report
    p_report = sub.add_parser("report", help="Show latest governance report.")
    p_report.add_argument("--format", choices=["ascii", "json", "html"], default="ascii")

    # flags
    p_flags = sub.add_parser("flags", help="List governance flags.")
    p_flags.add_argument("--all", action="store_true", help="Include resolved flags.")

    # show
    p_show = sub.add_parser("show", help="Show flag details.")
    p_show.add_argument("flag_id", help="Flag ID (or suffix).")

    # keep / delete / adjust / ignore
    for cmd_name in ("keep", "delete", "adjust", "ignore"):
        p = sub.add_parser(cmd_name, help=f"Mark flag as '{cmd_name}'.")
        p.add_argument("flag_id", help="Flag ID (or suffix).")

    # run
    p_run = sub.add_parser("run", help="Trigger a governance cycle.")
    p_run.add_argument("--dry", action="store_true", help="Dry run (skip writes).")
    p_run.add_argument("--mode", choices=["normal", "team"], default="normal")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "report": cmd_report,
        "flags": cmd_flags,
        "show": cmd_show,
        "keep": cmd_keep,
        "delete": cmd_delete,
        "adjust": cmd_adjust,
        "ignore": cmd_ignore,
        "run": cmd_run,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 1
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
