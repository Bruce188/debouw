"""
Circuit breaker pattern for ingest pipeline calls.

Stops ingestion after N consecutive failures to prevent runaway scrape
attempts or API abuse during outages.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from textwrap import dedent
from typing import Optional, Tuple
import logging


@dataclass
class CircuitBreaker:
    """
    Circuit breaker to halt ingestion after consecutive failures.

    States:
    - CLOSED: Normal operation, scrapes allowed
    - OPEN: Too many failures, scrapes blocked
    - HALF_OPEN: Testing if system recovered
    """

    max_failures: int = 5
    reset_timeout_minutes: int = 30

    # Internal state
    failure_count: int = field(default=0, init=False)
    last_failure_time: Optional[datetime] = field(default=None, init=False)
    state: str = field(default="CLOSED", init=False)

    def __post_init__(self):
        self.logger = logging.getLogger(__name__)

    def record_success(self) -> None:
        """Record a successful scrape, reset failure count."""
        self.failure_count = 0
        self.state = "CLOSED"
        self.logger.debug("Circuit breaker: Success recorded, state CLOSED")

    def record_failure(self) -> None:
        """Record a failed scrape, potentially open circuit."""
        self.failure_count += 1
        self.last_failure_time = datetime.now(timezone.utc)

        if self.failure_count >= self.max_failures:
            self.state = "OPEN"
            self.logger.warning(dedent(f"""
                Circuit breaker OPEN after {self.failure_count} consecutive failures.
                Ingest halted for {self.reset_timeout_minutes} minutes.
            """).strip())

    def can_execute(self) -> Tuple[bool, str]:
        """
        Check if ingestion is allowed.

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        if self.state == "CLOSED":
            return True, "Circuit closed, ingestion allowed"

        if self.state == "OPEN":
            # Check if timeout has passed
            if self.last_failure_time:
                elapsed = datetime.now(timezone.utc) - self.last_failure_time
                if elapsed >= timedelta(minutes=self.reset_timeout_minutes):
                    self.state = "HALF_OPEN"
                    self.logger.info("Circuit breaker: Timeout elapsed, state HALF_OPEN")
                    return True, "Circuit half-open, testing recovery"

            remaining = self.reset_timeout_minutes
            if self.last_failure_time:
                elapsed_mins = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds() / 60
                remaining = max(0, self.reset_timeout_minutes - elapsed_mins)

            return False, dedent(f"""
                Circuit breaker OPEN.
                {self.failure_count} consecutive failures.
                Resume ingest in {remaining:.1f} minutes.
            """).strip()

        if self.state == "HALF_OPEN":
            return True, "Circuit half-open, testing recovery"

        return False, f"Unknown circuit state: {self.state}"

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"
        self.logger.info("Circuit breaker manually reset")

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "max_failures": self.max_failures,
            "last_failure_time": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "reset_timeout_minutes": self.reset_timeout_minutes,
        }


