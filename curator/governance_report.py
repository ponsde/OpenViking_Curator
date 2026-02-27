"""Governance Report: format governance cycle results for humans / machines.

Supports three output formats (same pattern as decision_report.py):
- ASCII box (terminal)
- JSON (API / Loki)
- HTML (web / email)

And two detail levels:
- normal: overview + key findings
- team:   normal + full audit trail, TTL details, config snapshot
"""

from __future__ import annotations

import html as _html
import json
import unicodedata

_WIDTH = 64


# ── Display helpers (reused from decision_report.py pattern) ─────────────────


def _display_width(s: str) -> int:
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def _pad_to(s: str, width: int) -> str:
    dw = _display_width(s)
    if dw < width:
        s += " " * (width - dw)
    return s


def _truncate_to(s: str, width: int) -> str:
    """Truncate string to fit within display width, CJK-safe."""
    result: list[str] = []
    w = 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:  # reserve 1 col for ellipsis
            break
        result.append(ch)
        w += cw
    return "".join(result) + "…"


def _row(label: str, value: str, width: int = _WIDTH) -> str:
    inner = f" {label:<20}: {value}"
    dw = _display_width(inner)
    if dw > width:
        inner = _truncate_to(inner, width)
    inner = _pad_to(inner, width)
    return f"│{inner}│"


def _section_header(title: str, width: int = _WIDTH) -> str:
    pad = width - _display_width(title) - 2
    left = pad // 2
    right = pad - left
    return f"├{'─' * left} {title} {'─' * right}┤"


# ── ASCII format ─────────────────────────────────────────────────────────────


def format_report(report: dict) -> str:
    """Format governance report as ASCII box for terminal display."""
    overview = report.get("overview", {})
    health = report.get("knowledge_health", {})
    flags = report.get("flags", {})
    proactive = report.get("proactive", {})
    mode = report.get("mode", "normal")

    border = "─" * (_WIDTH + 2)
    title = " Governance Report "
    pad_left = (_WIDTH - len(title)) // 2
    pad_right = _WIDTH - len(title) - pad_left
    header = f"┌{'─' * pad_left}{title}{'─' * pad_right}┐"
    footer = f"└{border[1:-1]}┘"

    lines = [header]

    # Overview
    lines.append(_row("Cycle ID", report.get("cycle_id", "?")))
    lines.append(_row("Date", report.get("timestamp", "?")[:19]))
    lines.append(_row("Mode", mode))
    lines.append(_row("Total Resources", str(overview.get("total_resources", 0))))

    hs = overview.get("health_score", -1)
    health_label = f"{hs}/100" if hs >= 0 else "unknown"
    lines.append(_row("Health Score", health_label))

    # Knowledge health
    lines.append(_section_header("Knowledge Health"))
    lines.append(_row("Fresh", str(health.get("fresh", 0))))
    lines.append(_row("Aging", str(health.get("aging", 0))))
    lines.append(_row("Stale", str(health.get("stale", 0))))
    cov_mean = health.get("coverage_mean", 0)
    lines.append(_row("Coverage (mean)", f"{cov_mean:.3f}" if cov_mean else "N/A"))

    # Flags
    lines.append(_section_header("Flags"))
    lines.append(_row("Total Flags", str(flags.get("total", 0))))
    for ft, count in (flags.get("by_type") or {}).items():
        lines.append(_row(f"  {ft}", str(count)))

    # Proactive
    lines.append(_section_header("Proactive Search"))
    if proactive.get("dry_run"):
        lines.append(_row("Status", "SKIPPED (dry run)"))
    else:
        lines.append(_row("Sync Queries", str(proactive.get("queries_run", 0))))
        lines.append(_row("Sync Ingested", str(proactive.get("ingested", 0))))
        async_q = proactive.get("async_queued", 0)
        if async_q:
            lines.append(_row("Async Queued", str(async_q)))

    # Async harvest (from previous cycle)
    async_harvest = report.get("async_harvest", {})
    harvested = async_harvest.get("harvested", 0)
    if harvested:
        lines.append(_section_header("Async Harvest"))
        lines.append(_row("Harvested", str(harvested)))
        lines.append(_row("Ingested", str(async_harvest.get("ingested", 0))))

    # Pending review
    lines.append(_row("Pending Reviews", str(report.get("pending_review_count", 0))))

    # Weak topics
    weak = report.get("weak_topics", [])
    if weak:
        lines.append(_section_header("Top Weak Topics"))
        for t in weak[:5]:
            topic = t.get("topic", "?")[:30]
            cov = t.get("avg_coverage", 0)
            lines.append(_row(f"  {topic}", f"cov={cov:.2f}"))

    # Duration
    dur = report.get("duration_sec")
    if dur is not None:
        lines.append(_row("Duration", f"{dur:.1f}s"))

    lines.append(footer)
    return "\n".join(lines)


def format_report_json(report: dict) -> str:
    """Return governance report as formatted JSON string."""
    return json.dumps(report, ensure_ascii=False, indent=2)


def format_report_html(report: dict) -> str:
    """Return governance report as HTML fragment."""
    overview = report.get("overview", {})
    health = report.get("knowledge_health", {})
    flags = report.get("flags", {})
    proactive = report.get("proactive", {})
    mode = report.get("mode", "normal")

    def _tr(label: str, value: str) -> str:
        return (
            f"  <tr>"
            f"<th style='text-align:left;padding:2px 8px'>{_html.escape(label)}</th>"
            f"<td style='padding:2px 8px'>{_html.escape(value)}</td>"
            f"</tr>"
        )

    def _section(title: str) -> str:
        return (
            f"  <tr><th colspan='2' style='text-align:left;padding:8px 8px 2px;"
            f"font-size:14px;border-top:1px solid #ccc'>{_html.escape(title)}</th></tr>"
        )

    hs = overview.get("health_score", -1)
    health_label = f"{hs}/100" if hs >= 0 else "unknown"

    rows = [
        _tr("Cycle ID", report.get("cycle_id", "?")),
        _tr("Date", report.get("timestamp", "?")[:19]),
        _tr("Mode", mode),
        _tr("Total Resources", str(overview.get("total_resources", 0))),
        _tr("Health Score", health_label),
        _section("Knowledge Health"),
        _tr("Fresh", str(health.get("fresh", 0))),
        _tr("Aging", str(health.get("aging", 0))),
        _tr("Stale", str(health.get("stale", 0))),
        _section("Flags"),
        _tr("Total Flags", str(flags.get("total", 0))),
    ]

    for ft, count in (flags.get("by_type") or {}).items():
        rows.append(_tr(f"  {ft}", str(count)))

    rows.append(_section("Proactive Search"))
    if proactive.get("dry_run"):
        rows.append(_tr("Status", "SKIPPED (dry run)"))
    else:
        rows.append(_tr("Sync Queries", str(proactive.get("queries_run", 0))))
        rows.append(_tr("Sync Ingested", str(proactive.get("ingested", 0))))
        async_q = proactive.get("async_queued", 0)
        if async_q:
            rows.append(_tr("Async Queued", str(async_q)))

    # Async harvest
    async_harvest = report.get("async_harvest", {})
    harvested = async_harvest.get("harvested", 0)
    if harvested:
        rows.append(_section("Async Harvest"))
        rows.append(_tr("Harvested", str(harvested)))
        rows.append(_tr("Ingested", str(async_harvest.get("ingested", 0))))

    rows.append(_tr("Pending Reviews", str(report.get("pending_review_count", 0))))

    # Duration
    dur = report.get("duration_sec")
    if dur is not None:
        rows.append(_tr("Duration", f"{dur:.1f}s"))

    # Team mode extras
    if mode == "team":
        config = report.get("config_snapshot", {})
        if config:
            rows.append(_section("Config Snapshot"))
            for k, v in config.items():
                rows.append(_tr(k, str(v)))

        audit = report.get("audit_log", [])
        if audit:
            rows.append(_section(f"Audit Log ({len(audit)} entries)"))
            for entry in audit[:20]:
                label = f"[{entry.get('phase', '?')}] {entry.get('action', '?')}"
                rows.append(_tr(label, entry.get("outcome", "")))

    inner = "\n".join(rows)
    return (
        '<div class="curator-governance-report">\n'
        '<table style="border-collapse:collapse;font-family:monospace;font-size:13px">\n'
        f"  <tr><th colspan='2' style='text-align:center;padding:8px;"
        f"font-size:16px'>Governance Report</th></tr>\n"
        f"{inner}\n"
        "</table>\n"
        "</div>"
    )
