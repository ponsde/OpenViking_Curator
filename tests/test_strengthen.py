from __future__ import annotations

import json


def _write_weak_topics(path, topics):
    path.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")


def test_strengthen_dry_run(tmp_data_dir):
    from scripts.strengthen import strengthen

    weak_path = tmp_data_dir / "weak_topics.json"
    _write_weak_topics(
        weak_path,
        [
            {"topic": "docker", "avg_coverage": 0.2, "external_rate": 0.9},
            {"topic": "nginx", "avg_coverage": 0.3, "external_rate": 0.8},
        ],
    )

    out = strengthen(str(tmp_data_dir), top_n=1, dry=True)
    assert len(out) == 1
    assert out[0]["topic"] == "docker"
    assert out[0]["status"] == "dry_run"


def test_strengthen_exec_path(tmp_data_dir, monkeypatch):
    from scripts.strengthen import strengthen

    weak_path = tmp_data_dir / "weak_topics.json"
    _write_weak_topics(
        weak_path,
        [{"topic": "redis", "avg_coverage": 0.1, "external_rate": 1.0}],
    )

    calls = []

    def _fake_run(query):
        calls.append(query)
        return {
            "coverage": 0.78,
            "meta": {
                "external_triggered": True,
                "ingested": True,
            },
        }

    monkeypatch.setattr("curator.pipeline_v2.run", _fake_run)
    monkeypatch.setattr("scripts.strengthen.time.sleep", lambda *_: None)

    out = strengthen(str(tmp_data_dir), top_n=1, dry=False)
    assert len(out) == 1
    assert out[0]["status"] == "ok"
    assert out[0]["ingested"] is True
    assert calls and "redis" in calls[0]
    assert (tmp_data_dir / "strengthen_report.json").exists()
