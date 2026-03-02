"""Tests for curator.metrics — elapsed_ms timing."""

import time

from curator.metrics import Metrics


def _make_metrics(tmp_path):
    """Create a Metrics instance writing to a temp file."""
    return Metrics(path=str(tmp_path / "report.jsonl"))


def test_elapsed_ms_present_in_extra(tmp_path):
    """elapsed_ms must appear in every step's extra dict."""
    m = _make_metrics(tmp_path)
    m.step("init")
    assert "elapsed_ms" in m.data["steps"][0]["extra"]


def test_first_step_elapsed_from_started_at(tmp_path):
    """First step's elapsed_ms should be measured from started_at."""
    m = _make_metrics(tmp_path)
    started = m.data["started_at"]
    time.sleep(0.05)
    m.step("init")

    step = m.data["steps"][0]
    elapsed = step["extra"]["elapsed_ms"]
    expected = (step["ts"] - started) * 1000

    assert elapsed >= 40  # at least ~50ms minus tolerance
    assert abs(elapsed - expected) < 5  # should match closely


def test_consecutive_steps_elapsed_independent(tmp_path):
    """Each step measures elapsed from the previous step, not from started_at."""
    m = _make_metrics(tmp_path)
    time.sleep(0.05)
    m.step("first")
    time.sleep(0.08)
    m.step("second")

    first = m.data["steps"][0]
    second = m.data["steps"][1]

    # First step: elapsed from started_at (~50ms)
    assert first["extra"]["elapsed_ms"] >= 40

    # Second step: elapsed from first step (~80ms), NOT from started_at
    assert second["extra"]["elapsed_ms"] >= 70
    assert second["extra"]["elapsed_ms"] < 200  # sanity upper bound


def test_finalize_all_steps_have_elapsed_ms(tmp_path, monkeypatch):
    """After finalize, every step in data must contain elapsed_ms."""
    m = _make_metrics(tmp_path)
    m.step("a")
    m.step("b", ok=False, extra={"reason": "test"})
    m.step("c", extra={"foo": "bar"})

    monkeypatch.setattr("curator.metrics.Path.parent", tmp_path, raising=False)
    result = m.finalize()

    assert len(result["steps"]) == 3
    for step in result["steps"]:
        assert "elapsed_ms" in step["extra"]
        assert isinstance(step["extra"]["elapsed_ms"], float)


def test_caller_extra_not_mutated(tmp_path):
    """The caller's extra dict must not be mutated by step()."""
    m = _make_metrics(tmp_path)
    caller_extra = {"key": "value"}
    m.step("init", extra=caller_extra)

    # Caller's dict should be untouched
    assert "elapsed_ms" not in caller_extra
    assert caller_extra == {"key": "value"}

    # But the step's extra should have both
    step_extra = m.data["steps"][0]["extra"]
    assert step_extra["key"] == "value"
    assert "elapsed_ms" in step_extra


def test_caller_extra_preserved(tmp_path):
    """Caller-provided extra keys must survive alongside elapsed_ms."""
    m = _make_metrics(tmp_path)
    m.step("search", extra={"len": 42, "cache": "hit"})

    extra = m.data["steps"][0]["extra"]
    assert extra["len"] == 42
    assert extra["cache"] == "hit"
    assert "elapsed_ms" in extra


def test_elapsed_ms_is_nonnegative(tmp_path):
    """elapsed_ms should always be >= 0."""
    m = _make_metrics(tmp_path)
    m.step("a")
    m.step("b")
    m.step("c")

    for step in m.data["steps"]:
        assert step["extra"]["elapsed_ms"] >= 0
