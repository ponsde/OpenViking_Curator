"""Tests for curator.circuit_breaker — lightweight circuit breaker."""

import time

import pytest


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Reset all circuit breakers between tests."""
    from curator.circuit_breaker import reset_all

    reset_all()
    yield
    reset_all()


@pytest.fixture(autouse=True)
def _enable_cb(monkeypatch):
    """Ensure circuit breaker is enabled for tests."""
    monkeypatch.setattr("curator.circuit_breaker.CB_ENABLED", True)
    monkeypatch.setattr("curator.circuit_breaker.CB_THRESHOLD", 3)
    monkeypatch.setattr("curator.circuit_breaker.CB_RECOVERY_SEC", 30)


class TestStateMachine:
    def test_starts_closed(self):
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test")
        assert cb.state == State.CLOSED

    def test_stays_closed_below_threshold(self):
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == State.CLOSED
        assert cb.allow_request()

    def test_trips_to_open_at_threshold(self):
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == State.OPEN
        assert not cb.allow_request()

    def test_open_to_half_open_after_recovery(self, monkeypatch):
        import curator.circuit_breaker as cb_mod
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=2, recovery_sec=10)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == State.OPEN

        # Fast-forward time past recovery
        future = time.time() + 15
        monkeypatch.setattr(cb_mod.time, "time", lambda: future)
        assert cb.state == State.HALF_OPEN

    def test_half_open_success_closes(self, monkeypatch):
        import curator.circuit_breaker as cb_mod
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=2, recovery_sec=5)
        cb.record_failure()
        cb.record_failure()

        # Fast-forward past recovery
        future = time.time() + 10
        monkeypatch.setattr(cb_mod.time, "time", lambda: future)

        # Half-open allows one probe
        assert cb.allow_request()
        cb.record_success()
        assert cb.state == State.CLOSED

    def test_half_open_failure_reopens(self, monkeypatch):
        import curator.circuit_breaker as cb_mod
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=1, recovery_sec=5)
        cb.record_failure()
        assert cb.state == State.OPEN

        # Fast-forward past recovery
        future = time.time() + 10
        monkeypatch.setattr(cb_mod.time, "time", lambda: future)

        assert cb.allow_request()  # probe allowed
        cb.record_failure()
        assert cb.state == State.OPEN

    def test_success_resets_failure_count(self):
        from curator.circuit_breaker import CircuitBreaker, State

        cb = CircuitBreaker("test", threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # Still closed because success reset the counter
        assert cb.state == State.CLOSED


class TestDisabled:
    def test_always_allows_when_disabled(self, monkeypatch):
        monkeypatch.setattr("curator.circuit_breaker.CB_ENABLED", False)
        from curator.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker("test", threshold=1)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.allow_request()


class TestRegistry:
    def test_get_breaker_singleton(self):
        from curator.circuit_breaker import get_breaker

        b1 = get_breaker("svc-a")
        b2 = get_breaker("svc-a")
        assert b1 is b2

    def test_different_names_different_instances(self):
        from curator.circuit_breaker import get_breaker

        b1 = get_breaker("svc-a")
        b2 = get_breaker("svc-b")
        assert b1 is not b2

    def test_reset_all_clears_registry(self):
        from curator.circuit_breaker import State, get_breaker, reset_all

        b = get_breaker("svc-x")
        b.record_failure()
        b.record_failure()
        b.record_failure()
        assert b.state == State.OPEN
        reset_all()
        b2 = get_breaker("svc-x")
        assert b2.state == State.CLOSED
        assert b2 is not b


class TestChatIntegration:
    def test_chat_records_failure_on_error(self, monkeypatch):
        """config.chat() should record failure on the circuit breaker."""
        import requests

        from curator.circuit_breaker import State, get_breaker

        monkeypatch.setattr("curator.config.CHAT_RETRY_MAX", 1)

        def mock_post(*args, **kwargs):
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr("curator.config.requests.post", mock_post)

        from curator.config import chat

        with pytest.raises(RuntimeError):
            chat("http://fake", "key", "model-x", [{"role": "user", "content": "hi"}])

        breaker = get_breaker("chat:model-x")
        assert breaker._failure_count == 1

    def test_chat_raises_circuit_open(self, monkeypatch):
        """When circuit is open, chat() should raise CircuitOpenError immediately."""
        from curator.circuit_breaker import CircuitOpenError, get_breaker

        breaker = get_breaker("chat:model-y")
        # Trip the breaker
        for _ in range(3):
            breaker.record_failure()

        from curator.config import chat

        with pytest.raises(CircuitOpenError):
            chat("http://fake", "key", "model-y", [{"role": "user", "content": "hi"}])


class TestSearchProviderIntegration:
    def test_open_provider_skipped(self, monkeypatch):
        """search() should skip providers whose circuit is open."""
        from curator.circuit_breaker import get_breaker

        # Trip grok breaker
        b = get_breaker("search:grok")
        for _ in range(3):
            b.record_failure()

        call_log = []

        def mock_call(pname, query, scope):
            call_log.append(pname)
            return f"result from {pname}"

        monkeypatch.setattr("curator.search_providers._call_provider", mock_call)
        monkeypatch.setattr("curator.search_providers._get_provider_chain", lambda: ["grok", "duckduckgo"])

        from curator.search_providers import search

        result = search("test query", {})
        assert "duckduckgo" in call_log
        assert "grok" not in call_log
        assert result == "result from duckduckgo"
