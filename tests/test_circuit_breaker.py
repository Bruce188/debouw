"""
State machine tests for debouw/ingest/circuit_breaker.py.

Mirrors the apex_omni circuit breaker test surface. All tests are deterministic
— no real time.sleep; timeout transitions are tested via direct attribute
manipulation on _opened_at / last_failure_time.
"""

import pytest
from datetime import datetime, timedelta, timezone

from debouw.ingest.circuit_breaker import CircuitBreaker


class CircuitBreakerState:
    """Readable state constants used only within this test module.

    The production CircuitBreaker uses plain string literals ("CLOSED", "OPEN",
    "HALF_OPEN") to stay a verbatim copy of the apex_omni source. These constants
    live here — not in the module — so the verbatim-copy contract is preserved.
    """
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


def test_initial_state_is_closed():
    """Fresh CircuitBreaker starts CLOSED and permits execution."""
    cb = CircuitBreaker()
    assert cb.state == CircuitBreakerState.CLOSED
    allowed, reason = cb.can_execute()
    assert allowed is True
    assert "allowed" in reason.lower() or "closed" in reason.lower()


def test_transitions_to_open_at_max_failures():
    """After max_failures=5 consecutive failures, state becomes OPEN."""
    cb = CircuitBreaker()
    for _ in range(cb.max_failures):
        cb.record_failure()
    assert cb.state == CircuitBreakerState.OPEN
    allowed, reason = cb.can_execute()
    assert allowed is False
    assert "OPEN" in reason or "failures" in reason


def test_transitions_to_half_open_after_timeout(monkeypatch):
    """After reset_timeout_minutes elapses, OPEN → HALF_OPEN on can_execute()."""
    cb = CircuitBreaker()
    # Force to OPEN
    for _ in range(cb.max_failures):
        cb.record_failure()
    assert cb.state == CircuitBreakerState.OPEN

    # Backdate last_failure_time so timeout appears elapsed
    past = datetime.now(timezone.utc) - timedelta(minutes=cb.reset_timeout_minutes + 1)
    cb.last_failure_time = past

    allowed, reason = cb.can_execute()
    assert allowed is True
    assert cb.state == CircuitBreakerState.HALF_OPEN


def test_half_open_to_closed_on_success():
    """HALF_OPEN → CLOSED after record_success(); failure counter resets to 0."""
    cb = CircuitBreaker()
    # Manually put into HALF_OPEN
    cb.state = CircuitBreakerState.HALF_OPEN
    cb.failure_count = 5

    cb.record_success()
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 0


def test_half_open_to_open_on_failure():
    """HALF_OPEN → OPEN after record_failure() (probe failed)."""
    cb = CircuitBreaker()
    cb.state = CircuitBreakerState.HALF_OPEN
    cb.failure_count = cb.max_failures - 1  # one below threshold

    cb.record_failure()
    # failure_count now >= max_failures
    assert cb.state == CircuitBreakerState.OPEN


def test_reset_clears_state():
    """Manual reset returns state to CLOSED and zeroes failure count."""
    cb = CircuitBreaker()
    for _ in range(cb.max_failures):
        cb.record_failure()
    assert cb.state == CircuitBreakerState.OPEN

    cb.reset()
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 0
    assert cb.last_failure_time is None


def test_get_status_shape():
    """get_status() returns a dict with the expected keys."""
    cb = CircuitBreaker()
    status = cb.get_status()
    assert isinstance(status, dict)
    assert "state" in status
    assert "failure_count" in status
    # The apex_omni source uses last_failure_time; ensure one of the timestamp keys exists
    timestamp_keys = {"last_failure_time", "opened_at", "last_failure_at"}
    assert timestamp_keys & set(status.keys()), (
        f"Expected one of {timestamp_keys} in status dict, got: {set(status.keys())}"
    )
