"""Lightweight circuit breaker — fast-fail when external APIs are down.

Three states:
- CLOSED: normal operation, requests flow through.
- OPEN: API is considered down, requests are rejected immediately.
- HALF_OPEN: after recovery period, one probe request is allowed through.

No external dependencies.  Thread-safe via threading.Lock.
"""

from __future__ import annotations

import enum
import threading
import time

from .config import CB_ENABLED, CB_RECOVERY_SEC, CB_THRESHOLD, log


class State(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit breaker is open and the request is rejected."""


class CircuitBreaker:
    """Per-service circuit breaker instance."""

    def __init__(self, name: str, threshold: int = CB_THRESHOLD, recovery_sec: float = CB_RECOVERY_SEC):
        self.name = name
        self.threshold = threshold
        self.recovery_sec = recovery_sec
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._failure_count = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> State:
        with self._lock:
            return self._evaluate_state()

    def _evaluate_state(self) -> State:
        """Evaluate current state (must be called under lock)."""
        if self._state == State.OPEN:
            if time.time() - self._opened_at >= self.recovery_sec:
                self._state = State.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through.

        Returns True if allowed, False if the circuit is open.
        In HALF_OPEN state, allows exactly one probe request.
        """
        if not CB_ENABLED:
            return True

        with self._lock:
            state = self._evaluate_state()
            if state == State.CLOSED:
                return True
            if state == State.HALF_OPEN:
                # Allow one probe, transition back to OPEN until result
                self._state = State.OPEN
                self._opened_at = time.time()
                return True
            # OPEN
            return False

    def record_success(self) -> None:
        """Record a successful request — resets to CLOSED."""
        with self._lock:
            self._failure_count = 0
            self._state = State.CLOSED

    def record_failure(self) -> None:
        """Record a failed request — may trip to OPEN."""
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.threshold:
                if self._state != State.OPEN:
                    log.warning("circuit breaker %r OPEN after %d failures", self.name, self._failure_count)
                self._state = State.OPEN
                self._opened_at = time.time()

    def reset(self) -> None:
        """Reset to CLOSED state (for testing)."""
        with self._lock:
            self._state = State.CLOSED
            self._failure_count = 0
            self._opened_at = 0.0


# ── Global registry (singleton per name) ──

_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    """Get or create a CircuitBreaker for the given service name."""
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CircuitBreaker(name)
        return _registry[name]


def reset_all() -> None:
    """Reset all circuit breakers (for testing)."""
    with _registry_lock:
        for cb in _registry.values():
            cb.reset()
        _registry.clear()
