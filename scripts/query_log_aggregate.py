#!/usr/bin/env python3
"""Aggregate query_log.jsonl into daily metrics.

Reads data/query_log.jsonl (or path via --input), produces a summary with:
- Coverage distribution (p25/p50/p75/p90)
- External trigger rate
- Ingest success rate
- Conflict rate
- need_fresh hit rate
- LLM call distribution

Usage:
    python3 scripts/query_log_aggregate.py
    python3 scripts/query_log_aggregate.py --input data/query_log.jsonl --output data/query_metrics.json
    python3 scripts/query_log_aggregate.py --json   # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_entries(path: Path) -> list[dict]:
    """Load and validate query log entries. Skips malformed lines."""
    entries = []
    if not path.exists():
        return entries
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or "query" not in entry:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            print(f"  warning: skipping malformed line {i}", file=sys.stderr)
    return entries


def percentile(values: list[float], p: float) -> float:
    """Simple percentile (nearest-rank)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return round(s[idx], 4)


def aggregate(entries: list[dict]) -> dict:
    """Compute aggregate metrics from query log entries."""
    n = len(entries)
    if n == 0:
        return {"total_queries": 0, "error": "no entries"}

    coverages = [e.get("coverage", 0) for e in entries]
    external_count = sum(1 for e in entries if e.get("external_triggered"))
    ingest_count = sum(1 for e in entries if e.get("ingested"))
    conflict_count = sum(1 for e in entries if e.get("has_conflict"))
    fresh_count = sum(1 for e in entries if e.get("need_fresh"))
    async_count = sum(1 for e in entries if e.get("async_ingest_pending"))
    llm_calls = [e.get("llm_calls", 0) for e in entries]

    # Coverage reason breakdown
    reasons: dict[str, int] = {}
    for e in entries:
        r = e.get("reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    # Load stage breakdown
    stages: dict[str, int] = {}
    for e in entries:
        s = e.get("load_stage", "unknown")
        stages[s] = stages.get(s, 0) + 1

    # Schema version distribution
    schema_versions: dict[str, int] = {}
    for e in entries:
        sv = str(e.get("schema_version", 1))
        schema_versions[sv] = schema_versions.get(sv, 0) + 1

    return {
        "total_queries": n,
        "schema_versions": schema_versions,
        "coverage": {
            "mean": round(sum(coverages) / n, 4),
            "p25": percentile(coverages, 25),
            "p50": percentile(coverages, 50),
            "p75": percentile(coverages, 75),
            "p90": percentile(coverages, 90),
            "min": round(min(coverages), 4),
            "max": round(max(coverages), 4),
        },
        "rates": {
            "external_triggered": round(external_count / n, 4),
            "ingested": round(ingest_count / n, 4),
            "has_conflict": round(conflict_count / n, 4),
            "need_fresh": round(fresh_count / n, 4),
            "async_ingest": round(async_count / n, 4),
        },
        "llm_calls": {
            "mean": round(sum(llm_calls) / n, 2),
            "max": max(llm_calls),
            "total": sum(llm_calls),
        },
        "coverage_reasons": reasons,
        "load_stages": stages,
    }


def format_report(metrics: dict) -> str:
    """Format metrics as human-readable text."""
    if metrics.get("error"):
        return f"No data: {metrics['error']}"

    lines = [
        f"Query Log Aggregate ({metrics['total_queries']} queries)",
        "=" * 50,
        "",
        "Coverage distribution:",
        f"  mean={metrics['coverage']['mean']:.3f}  "
        f"p50={metrics['coverage']['p50']:.3f}  "
        f"p90={metrics['coverage']['p90']:.3f}  "
        f"range=[{metrics['coverage']['min']:.3f}, {metrics['coverage']['max']:.3f}]",
        "",
        "Rates:",
        f"  external:  {metrics['rates']['external_triggered']:.1%}",
        f"  ingested:  {metrics['rates']['ingested']:.1%}",
        f"  conflict:  {metrics['rates']['has_conflict']:.1%}",
        f"  fresh:     {metrics['rates']['need_fresh']:.1%}",
        f"  async:     {metrics['rates']['async_ingest']:.1%}",
        "",
        f"LLM calls: mean={metrics['llm_calls']['mean']:.1f}  total={metrics['llm_calls']['total']}",
        "",
        "Coverage reasons:",
    ]
    for reason, count in sorted(metrics["coverage_reasons"].items(), key=lambda x: -x[1]):
        lines.append(f"  {reason}: {count}")

    lines.append("")
    lines.append("Load stages:")
    for stage, count in sorted(metrics["load_stages"].items(), key=lambda x: -x[1]):
        lines.append(f"  {stage}: {count}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate query_log.jsonl metrics")
    parser.add_argument("--input", default="data/query_log.jsonl", help="Path to query_log.jsonl")
    parser.add_argument("--output", default=None, help="Write JSON metrics to file")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    entries = load_entries(Path(args.input))
    metrics = aggregate(entries)

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(format_report(metrics))

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
