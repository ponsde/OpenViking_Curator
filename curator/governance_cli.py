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


_VALID_FLAG_TYPES = ("stale_resource", "broken_url", "review_expired", "ttl_rebalance")
_VALID_SEVERITIES = ("low", "medium", "high")


def cmd_flags(args: argparse.Namespace) -> int:
    """List governance flags."""
    from .governance import load_flags

    # Validate filter args
    filter_type = getattr(args, "type", None)
    filter_severity = getattr(args, "severity", None)
    filter_cycle = getattr(args, "cycle", None)

    if filter_type is not None and filter_type not in _VALID_FLAG_TYPES:
        print(
            f"[error] invalid --type {filter_type!r}. Valid values: {', '.join(_VALID_FLAG_TYPES)}",
            file=sys.stderr,
        )
        return 1
    if filter_severity is not None and filter_severity not in _VALID_SEVERITIES:
        print(
            f"[error] invalid --severity {filter_severity!r}. Valid values: {', '.join(_VALID_SEVERITIES)}",
            file=sys.stderr,
        )
        return 1

    data_dir = args.data_path or DATA_PATH
    show_all = getattr(args, "all", False)
    status_filter = None if show_all else "pending"
    flags = load_flags(
        data_path=data_dir,
        status=status_filter,
        flag_type=filter_type,
        severity=filter_severity,
        cycle_id=filter_cycle,
    )

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
    # Show resolution info for non-pending flags
    if flag.get("status") not in ("pending", None):
        res_reason = flag.get("resolution_reason")
        res_at = flag.get("resolved_at")
        if res_reason is not None:
            print(f"decision: {res_reason}")
        if res_at is not None:
            print(f"resolved: {res_at}")
    return 0


def _update_flag_cmd(args: argparse.Namespace, new_status: str) -> int:
    """Generic flag status updater — supports multi-ID and filter-based batch."""
    from .governance import batch_update_flags, load_flags

    data_dir = args.data_path or DATA_PATH
    reason = getattr(args, "reason", None)
    use_batch = getattr(args, "batch", False)
    filter_type = getattr(args, "type", None)
    filter_severity = getattr(args, "severity", None)
    flag_ids: list[str] = list(getattr(args, "flag_ids", []) or [])

    # ── Filter-based batch mode ────────────────────────────────────────────────
    if use_batch or (filter_type is not None or filter_severity is not None):
        if not use_batch:
            print(
                "[error] use --batch to confirm filter-based batch operations. " "Add --batch to proceed.",
                file=sys.stderr,
            )
            return 1

        # Validate filter values
        if filter_type is not None and filter_type not in _VALID_FLAG_TYPES:
            print(
                f"[error] invalid --type {filter_type!r}. Valid values: {', '.join(_VALID_FLAG_TYPES)}",
                file=sys.stderr,
            )
            return 1
        if filter_severity is not None and filter_severity not in _VALID_SEVERITIES:
            print(
                f"[error] invalid --severity {filter_severity!r}. Valid values: {', '.join(_VALID_SEVERITIES)}",
                file=sys.stderr,
            )
            return 1

        # Load matching pending flags
        matching = load_flags(
            data_path=data_dir,
            status="pending",
            flag_type=filter_type,
            severity=filter_severity,
        )
        if not matching:
            print("(no matching pending flags found)")
            return 0

        # Show list before acting
        print(f"Matching {len(matching)} pending flag(s):")
        for f in matching:
            print(f"  {f.get('flag_id', '?')[-12:]}  {f.get('flag_type', '?')}  {f.get('uri', '?')[:60]}")

        target_ids = [f["flag_id"] for f in matching]
        updated, _ = batch_update_flags(target_ids, new_status, reason=reason, data_path=data_dir)
        print(f"Updated {len(updated)} flag(s) → {new_status}")
        return 0

    # ── Multi-ID mode ─────────────────────────────────────────────────────────
    if not flag_ids:
        print("[error] provide at least one flag ID, or use --type/--severity with --batch", file=sys.stderr)
        return 1

    # Resolve each ID (allow partial suffix match)
    all_flags = load_flags(data_path=data_dir)
    resolved: list[str] = []
    had_error = False
    for raw_id in flag_ids:
        matches = [f for f in all_flags if f.get("flag_id", "").endswith(raw_id)]
        if not matches:
            print(f"[warning] flag not found: {raw_id}", file=sys.stderr)
            had_error = True
            continue
        if len(matches) > 1:
            print(
                f"[warning] ambiguous flag_id '{raw_id}' matches {len(matches)} flags:",
                file=sys.stderr,
            )
            for m in matches:
                print(f"  {m['flag_id']}  ({m.get('flag_type', '?')}  {m.get('uri', '?')[:50]})", file=sys.stderr)
            print("  Skipped — provide a longer or full flag ID.", file=sys.stderr)
            had_error = True
            continue
        resolved.append(matches[0]["flag_id"])

    if not resolved:
        print("[error] no valid flag IDs to update", file=sys.stderr)
        return 1

    updated, not_found = batch_update_flags(resolved, new_status, reason=reason, data_path=data_dir)
    for fid in updated:
        print(f"Flag {fid} → {new_status}")
    for fid in not_found:
        print(f"[warning] failed to update flag {fid}", file=sys.stderr)
        had_error = True

    return 1 if (had_error and not updated) else 0


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
    p_flags.add_argument(
        "--type",
        dest="type",
        choices=list(_VALID_FLAG_TYPES),
        default=None,
        help="Filter by flag type.",
    )
    p_flags.add_argument(
        "--severity",
        dest="severity",
        choices=list(_VALID_SEVERITIES),
        default=None,
        help="Filter by severity.",
    )
    p_flags.add_argument("--cycle", dest="cycle", default=None, help="Filter by cycle ID.")

    # show
    p_show = sub.add_parser("show", help="Show flag details.")
    p_show.add_argument("flag_id", help="Flag ID (or suffix).")

    # keep / delete / adjust / ignore
    for cmd_name in ("keep", "delete", "adjust", "ignore"):
        p = sub.add_parser(cmd_name, help=f"Mark flag as '{cmd_name}'.")
        p.add_argument("flag_ids", nargs="*", help="Flag ID(s) (or suffix). Multiple supported.")
        p.add_argument("--reason", default=None, help="Decision reason (recorded in flag data).")
        p.add_argument(
            "--type",
            dest="type",
            choices=list(_VALID_FLAG_TYPES),
            default=None,
            help="Filter by flag type (use with --batch for bulk ops).",
        )
        p.add_argument(
            "--severity",
            dest="severity",
            choices=list(_VALID_SEVERITIES),
            default=None,
            help="Filter by severity (use with --batch for bulk ops).",
        )
        p.add_argument(
            "--batch",
            action="store_true",
            help="Apply to all matching pending flags (requires confirmation via flag).",
        )

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
