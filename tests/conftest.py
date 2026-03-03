"""Shared pytest fixtures for Curator tests (Phase 3)."""

from __future__ import annotations

import json
import sys

import pytest


@pytest.fixture
def memory_backend():
    """InMemoryBackend seeded with a couple of knowledge items."""
    from curator.backend_memory import InMemoryBackend

    backend = InMemoryBackend()
    backend.ingest("Docker deployment guide with nginx reverse proxy.", title="docker_nginx")
    backend.ingest("Python asyncio event loop and task scheduling notes.", title="python_asyncio")
    return backend


@pytest.fixture
def mock_llm(monkeypatch):
    """Patch LLM chat call sites with a controllable fake.

    Besides ``curator.config.chat``, this fixture also patches already-imported
    ``curator.*`` modules that bound ``chat`` at import time
    (``from curator.config import chat``).
    """
    import curator.config as cfg

    state = {
        "response": '{"pass": false, "reason": "mock"}',
        "calls": [],
    }

    def _fake_chat(base, key, model, messages, timeout=60, temperature=None):
        state["calls"].append(
            {
                "base": base,
                "key": key,
                "model": model,
                "messages": messages,
                "timeout": timeout,
                "temperature": temperature,
            }
        )
        return state["response"]

    # Patch canonical source.
    original_chat = cfg.chat
    monkeypatch.setattr("curator.config.chat", _fake_chat)

    # Patch already-imported modules that captured old chat reference.
    for name, mod in list(sys.modules.items()):
        if not name.startswith("curator"):
            continue
        if mod is None or not hasattr(mod, "chat"):
            continue
        try:
            if getattr(mod, "chat") is original_chat:
                monkeypatch.setattr(mod, "chat", _fake_chat)
        except Exception:
            # Best-effort patching; ignore unusual module objects.
            pass

    def _set_response(text: str):
        state["response"] = text

    return {
        "set_response": _set_response,
        "calls": state["calls"],
    }


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Temporary data dir + CURATOR_DATA_PATH env override."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CURATOR_DATA_PATH", str(data_dir))
    return data_dir


@pytest.fixture
def query_log(tmp_data_dir):
    """Seeded query_log.jsonl under tmp_data_dir."""
    log_path = tmp_data_dir / "query_log.jsonl"
    entries = [
        {
            "query": "docker deploy",
            "coverage": 0.62,
            "external_triggered": False,
            "ingested": False,
            "has_conflict": False,
            "need_fresh": False,
            "llm_calls": 0,
            "reason": "local_sufficient",
            "load_stage": "L0",
        },
        {
            "query": "latest redis release",
            "coverage": 0.31,
            "external_triggered": True,
            "ingested": True,
            "has_conflict": False,
            "need_fresh": True,
            "llm_calls": 2,
            "reason": "low_coverage",
            "load_stage": "L1",
        },
    ]
    with log_path.open("w", encoding="utf-8") as f:
        for row in entries:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return log_path
