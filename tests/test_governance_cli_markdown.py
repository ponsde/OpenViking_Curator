from __future__ import annotations

import argparse
import json


def test_build_parser_accepts_markdown():
    from curator.governance_cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["report", "--format", "markdown"])
    assert args.command == "report"
    assert args.format == "markdown"


def test_cmd_report_markdown_outputs_markdown(tmp_path, capsys):
    from curator.governance_cli import cmd_report

    report = {
        "cycle_id": "gov_cycle_20260227_100000",
        "timestamp": "2026-02-27T10:00:00+00:00",
        "mode": "normal",
        "overview": {"total_resources": 42, "health_score": 75},
        "knowledge_health": {"fresh": 30, "aging": 8, "stale": 4, "coverage_mean": 0.623},
        "flags": {"total": 0, "by_type": {}},
        "pending_flags": [],
        "pending_flags_total": 0,
        "proactive": {"queries_run": 3, "ingested": 2, "async_queued": 5, "dry_run": False},
        "pending_review_count": 2,
    }
    (tmp_path / "governance_report_latest.json").write_text(json.dumps(report), encoding="utf-8")

    args = argparse.Namespace(data_path=str(tmp_path), format="markdown")
    code = cmd_report(args)
    out = capsys.readouterr().out
    assert code == 0
    assert "# Governance Report" in out
    assert "## Overview" in out
    assert "无待处理 flag" in out
