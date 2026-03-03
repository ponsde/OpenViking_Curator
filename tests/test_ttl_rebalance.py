from __future__ import annotations

import json


def _meta_doc(*, freshness="current", ttl_days=180, usage_tier="warm", ingested="2026-02-01") -> str:
    return (
        f"<!-- curator_meta: ingested={ingested} freshness={freshness} ttl_days={ttl_days} usage_tier={usage_tier} -->\n"
        "# title\n"
    )


def test_ttl_rebalance_scan(tmp_path, monkeypatch):
    from scripts.ttl_rebalance import scan

    curated_dir = tmp_path / "curated"
    curated_dir.mkdir(parents=True)

    doc = curated_dir / "1700000000_docker_deploy.md"
    doc.write_text(_meta_doc(freshness="current", ttl_days=180, usage_tier="warm"), encoding="utf-8")

    fb_file = tmp_path / "feedback.json"
    fb_file.write_text(
        json.dumps({"viking://resources/docker-deploy-guide": {"adopt": 7, "up": 0, "down": 0}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))

    out = scan(str(curated_dir))
    assert len(out) == 1
    row = out[0]
    assert row["file"] == doc.name
    assert row["suggested_tier"] == "hot"
    assert row["suggested_ttl"] >= row["current_ttl"]


def test_ttl_rebalance_json_report(tmp_path, monkeypatch):
    from scripts import ttl_rebalance as mod

    curated_dir = tmp_path / "curated"
    curated_dir.mkdir(parents=True)
    (curated_dir / "1700000000_python_async.md").write_text(_meta_doc(), encoding="utf-8")

    fb_file = tmp_path / "feedback.json"
    fb_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_file))

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ttl_rebalance.py",
            "--json",
            "--top",
            "5",
            "--curated-dir",
            str(curated_dir),
            "--data-dir",
            str(data_dir),
        ],
    )

    mod.main()
    report = data_dir / "ttl_rebalance_report.json"
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert "items" in payload and isinstance(payload["items"], list)
