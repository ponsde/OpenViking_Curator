from __future__ import annotations

import json


def test_query_log_aggregate_metrics(query_log):
    from scripts.query_log_aggregate import aggregate, load_entries

    entries = load_entries(query_log)
    metrics = aggregate(entries)

    assert metrics["total_queries"] == 2
    assert metrics["rates"]["external_triggered"] == 0.5
    assert metrics["llm_calls"]["total"] == 2
    assert "coverage" in metrics and "p50" in metrics["coverage"]


def test_query_log_aggregate_skips_malformed(tmp_path):
    from scripts.query_log_aggregate import load_entries

    log = tmp_path / "query_log.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"query": "ok", "coverage": 0.3}),
                "{bad json",
                json.dumps({"not_query": True}),
            ]
        ),
        encoding="utf-8",
    )

    rows = load_entries(log)
    assert len(rows) == 1
    assert rows[0]["query"] == "ok"


def test_query_log_aggregate_cli_json(query_log, tmp_path, monkeypatch, capsys):
    from scripts import query_log_aggregate as mod

    out_path = tmp_path / "metrics.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "query_log_aggregate.py",
            "--input",
            str(query_log),
            "--output",
            str(out_path),
            "--json",
        ],
    )

    mod.main()
    printed = capsys.readouterr().out
    payload = json.loads(printed.split("\n\nSaved to", 1)[0])
    assert payload["total_queries"] == 2
    assert out_path.exists()
