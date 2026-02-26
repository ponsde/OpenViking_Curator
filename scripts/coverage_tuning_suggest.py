#!/usr/bin/env python3
"""Coverage tuning suggestions based on query_log data.

Analyzes query_log.jsonl to suggest threshold adjustments. Read-only:
never modifies config. Outputs suggestions with confidence levels.

Usage:
    python3 scripts/coverage_tuning_suggest.py
    python3 scripts/coverage_tuning_suggest.py --input data/query_log.jsonl --json
    python3 scripts/coverage_tuning_suggest.py --min-samples 50
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Import aggregate helpers from sibling script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from query_log_aggregate import load_entries, percentile

# Current defaults (from config.py)
DEFAULTS = {
    "CURATOR_THRESHOLD_COV_SUFFICIENT": 0.55,
    "CURATOR_THRESHOLD_COV_MARGINAL": 0.45,
    "CURATOR_THRESHOLD_COV_LOW": 0.35,
}

MIN_SAMPLES_LOW = 20
MIN_SAMPLES_MED = 50
MIN_SAMPLES_HIGH = 100


def analyze(entries: list[dict], current: dict | None = None) -> dict:
    """Analyze query log and produce tuning suggestions.

    Args:
        entries: Parsed query_log entries.
        current: Current threshold values. Uses DEFAULTS if None.

    Returns:
        Dict with suggestions, confidence, and reasoning.
    """
    if current is None:
        current = dict(DEFAULTS)

    n = len(entries)
    if n == 0:
        return {"error": "no data", "suggestions": []}

    # Split into groups by outcome
    external_entries = [e for e in entries if e.get("external_triggered")]
    local_entries = [e for e in entries if not e.get("external_triggered")]
    ingested_entries = [e for e in entries if e.get("ingested")]

    ext_rate = len(external_entries) / n
    ingest_rate = len(ingested_entries) / n if external_entries else 0

    # Coverage distributions per group
    all_cov = [e.get("coverage", 0) for e in entries]
    local_cov = [e.get("coverage", 0) for e in local_entries]

    # Confidence level based on sample size
    if n >= MIN_SAMPLES_HIGH:
        confidence = "high"
    elif n >= MIN_SAMPLES_MED:
        confidence = "medium"
    elif n >= MIN_SAMPLES_LOW:
        confidence = "low"
    else:
        confidence = "insufficient"

    suggestions = []

    # ── Suggestion 1: COV_SUFFICIENT threshold ──
    # If external triggers too often (>60%) and local results have decent coverage,
    # the threshold might be too high.
    if local_cov:
        local_p50 = percentile(local_cov, 50)
        cur_suf = current["CURATOR_THRESHOLD_COV_SUFFICIENT"]

        if ext_rate > 0.6 and local_p50 > 0.3:
            suggested = round(max(0.35, local_p50 - 0.05), 2)
            if suggested < cur_suf:
                suggestions.append(
                    {
                        "param": "CURATOR_THRESHOLD_COV_SUFFICIENT",
                        "current": cur_suf,
                        "suggested": suggested,
                        "direction": "lower",
                        "reason": (
                            f"External triggers {ext_rate:.0%} of queries. "
                            f"Local coverage p50={local_p50:.3f}. "
                            f"Lowering threshold may reduce unnecessary external calls."
                        ),
                    }
                )

        # If external rarely triggers (<20%) and many queries barely pass threshold,
        # the threshold might be too low (accepting low-quality local results).
        elif ext_rate < 0.2 and local_p50 < cur_suf + 0.1:
            suggested = round(min(0.75, local_p50 + 0.05), 2)
            if suggested > cur_suf:
                suggestions.append(
                    {
                        "param": "CURATOR_THRESHOLD_COV_SUFFICIENT",
                        "current": cur_suf,
                        "suggested": suggested,
                        "direction": "raise",
                        "reason": (
                            f"External triggers only {ext_rate:.0%}. "
                            f"Local coverage p50={local_p50:.3f} is close to threshold. "
                            f"Raising may improve answer quality by triggering external for marginal cases."
                        ),
                    }
                )

    # ── Suggestion 2: External search effectiveness ──
    if external_entries:
        ext_ingest_rate = len(ingested_entries) / len(external_entries)
        if ext_ingest_rate < 0.1 and len(external_entries) >= 10:
            suggestions.append(
                {
                    "param": "EXTERNAL_SEARCH_EFFECTIVENESS",
                    "current": f"{ext_ingest_rate:.1%} ingest rate from external",
                    "suggested": "review search providers or judge thresholds",
                    "direction": "investigate",
                    "reason": (
                        f"Only {ext_ingest_rate:.1%} of external searches lead to ingest. "
                        f"External search may be low quality or judge too strict."
                    ),
                }
            )

    # ── Suggestion 3: LLM call budget ──
    llm_calls = [e.get("llm_calls", 0) for e in entries]
    avg_llm = sum(llm_calls) / n
    if avg_llm > 1.5:
        suggestions.append(
            {
                "param": "LLM_CALL_BUDGET",
                "current": f"{avg_llm:.1f} avg calls/query",
                "suggested": "investigate need_fresh trigger rate",
                "direction": "investigate",
                "reason": (
                    f"Average {avg_llm:.1f} LLM calls per query. "
                    f"Target is <=1 for most queries. Check if need_fresh triggers too often."
                ),
            }
        )

    return {
        "sample_size": n,
        "confidence": confidence,
        "current_thresholds": current,
        "metrics": {
            "external_rate": round(ext_rate, 4),
            "ingest_rate": round(ingest_rate, 4),
            "coverage_p50": percentile(all_cov, 50),
            "coverage_p90": percentile(all_cov, 90),
            "avg_llm_calls": round(avg_llm, 2),
        },
        "suggestions": suggestions,
    }


def format_suggestions(result: dict) -> str:
    """Format suggestions as human-readable report."""
    if result.get("error"):
        return f"Cannot generate suggestions: {result['error']}"

    lines = [
        f"Coverage Tuning Suggestions (n={result['sample_size']}, confidence={result['confidence']})",
        "=" * 60,
        "",
        f"  external rate:  {result['metrics']['external_rate']:.1%}",
        f"  ingest rate:    {result['metrics']['ingest_rate']:.1%}",
        f"  coverage p50:   {result['metrics']['coverage_p50']:.3f}",
        f"  coverage p90:   {result['metrics']['coverage_p90']:.3f}",
        f"  avg LLM calls:  {result['metrics']['avg_llm_calls']:.1f}",
        "",
    ]

    if not result["suggestions"]:
        lines.append("No suggestions — current thresholds look balanced.")
    else:
        lines.append(f"{len(result['suggestions'])} suggestion(s):")
        lines.append("")
        for i, s in enumerate(result["suggestions"], 1):
            lines.append(f"  [{i}] {s['param']}")
            lines.append(f"      current:   {s['current']}")
            lines.append(f"      suggested: {s['suggested']}  ({s['direction']})")
            lines.append(f"      reason:    {s['reason']}")
            lines.append("")

    lines.append(f"Confidence: {result['confidence']} (based on {result['sample_size']} samples)")
    lines.append("NOTE: These are suggestions only. Review before applying.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Suggest coverage threshold tuning")
    parser.add_argument("--input", default="data/query_log.jsonl", help="Path to query_log.jsonl")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--min-samples", type=int, default=0, help="Minimum samples required")
    args = parser.parse_args()

    entries = load_entries(Path(args.input))

    if args.min_samples and len(entries) < args.min_samples:
        print(f"Insufficient data: {len(entries)} entries < {args.min_samples} minimum")
        sys.exit(1)

    result = analyze(entries)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_suggestions(result))


if __name__ == "__main__":
    main()
