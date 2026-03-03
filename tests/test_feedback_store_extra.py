from __future__ import annotations

import json


def test_feedback_store_save_and_load(tmp_path, monkeypatch):
    from curator import feedback_store

    fb_path = tmp_path / "fb.json"
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_path))

    payload = {"u": {"up": 1, "down": 0, "adopt": 0}}
    feedback_store.save(payload)
    got = feedback_store.load()
    assert got == payload


def test_feedback_store_load_corrupt_returns_empty(tmp_path, monkeypatch):
    from curator import feedback_store

    fb_path = tmp_path / "fb.json"
    fb_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_path))

    got = feedback_store.load()
    assert got == {}


def test_feedback_store_apply_invalid_action(tmp_path, monkeypatch):
    from curator import feedback_store

    fb_path = tmp_path / "fb.json"
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_path))

    try:
        feedback_store.apply("viking://x", "bad")
        assert False, "should raise"
    except ValueError:
        pass


def test_feedback_store_decay_factor_bad_timestamp():
    from curator.feedback_store import _decay_factor

    assert _decay_factor("not-a-time", 14.0) == 1.0


def test_feedback_store_resolve_path_from_settings(tmp_path, monkeypatch):
    from curator.feedback_store import _resolve_store

    fb_path = tmp_path / "x" / "feedback.json"
    monkeypatch.setenv("CURATOR_FEEDBACK_FILE", str(fb_path))
    assert _resolve_store() == fb_path
