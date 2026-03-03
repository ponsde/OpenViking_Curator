from __future__ import annotations

import json


def test_memory_backend_fixture(memory_backend):
    resp = memory_backend.find("docker", limit=5)
    assert resp.total >= 1


def test_mock_llm_fixture(mock_llm):
    from curator.config import chat

    mock_llm["set_response"]('{"ok": true}')
    out = chat("base", "key", "model", [{"role": "user", "content": "hi"}])
    assert out == '{"ok": true}'
    assert len(mock_llm["calls"]) == 1


def test_tmp_data_dir_fixture_sets_env(tmp_data_dir, monkeypatch):
    import os

    assert os.environ["CURATOR_DATA_PATH"] == str(tmp_data_dir)
    # Ensure override can still be changed in-test if needed
    monkeypatch.setenv("CURATOR_DATA_PATH", str(tmp_data_dir / "alt"))
    assert os.environ["CURATOR_DATA_PATH"].endswith("alt")


def test_query_log_fixture_seeded(query_log):
    lines = query_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    obj = json.loads(lines[0])
    assert "query" in obj and "coverage" in obj
