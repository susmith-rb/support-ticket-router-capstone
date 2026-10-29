"""Distributed resilience primitives: CircuitBreaker and latency budget helpers.

# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

# Latency budget constants per REQ-AGENT-012
ROUTER_S1_TIMEOUT_S: float = 0.500  # HTTP call timeout. Model budget is 150 ms (server p95 59 ms); e2e :rawPredict p95 ~189 ms in-region
TIER2_FLASH_TIMEOUT_S: float = 1.500  # 1500 ms
AGENT_SEARCH_TIMEOUT_S: float = 0.800  # 800 ms
LAYA_RERANK_TIMEOUT_S: float = 0.600  # HTTP call timeout; measured in-region e2e rerank p95 ~200 ms
ORDER_LOOKUP_MCP_TIMEOUT_S: float = 5.000  # 5000 ms (REQ-AGENT-012: accommodates min_instance_count=0 Cloud Run + Cloud SQL IAM cold starts while preserving <300 ms warm latency within 20s FAQ_OVERALL_BUDGET_S)
TICKET_DB_MCP_TIMEOUT_S: float = 0.500  # 500 ms
TRIAGE_OVERALL_BUDGET_S: float = 3.000  # 3000 ms
FAQ_OVERALL_BUDGET_S: float = 20.000  # 20000 ms


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerError(Exception):
    """Base error for circuit breaker operations."""


class CircuitBreakerOpenError(CircuitBreakerError):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""


class ServiceTimeoutError(TimeoutError, CircuitBreakerError):
    """Raised when an operation exceeds its latency budget."""


class CircuitBreaker:
    """In-memory thread-safe circuit breaker with exponential backoff and jitter.

    REQ-AGENT-011:
    - 3 consecutive failures -> OPEN state for 30 s base cooldown.
    - Exponential doubling up to 300 s cap.
    - ±20% random jitter on backoff cooldown.
    - HALF_OPEN state permits 1 canary probe.
    - 2 consecutive successes in HALF_OPEN to transition to CLOSED.
    - Fully injectable clock and rng for deterministic testing.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        success_threshold: int = 2,
        base_cooldown_s: float = 30.0,
        max_cooldown_s: float = 300.0,
        jitter_ratio: float = 0.20,
        clock: Optional[Callable[[], float]] = None,
        rng: Optional[Callable[[], float]] = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.base_cooldown_s = base_cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self.jitter_ratio = jitter_ratio
        self._clock = clock if clock is not None else time.monotonic
        self._rng = rng if rng is not None else random.random

        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._trip_count = 0
        self._opened_at = 0.0
        self._cooldown_s = 0.0
        self._reset_at = 0.0
        self._canary_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_state_transition()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def consecutive_successes(self) -> int:
        with self._lock:
            return self._consecutive_successes

    @property
    def trip_count(self) -> int:
        with self._lock:
            return self._trip_count

    @property
    def reset_at(self) -> float:
        with self._lock:
            return self._reset_at

    def _check_state_transition(self) -> None:
        """Evaluate whether an OPEN breaker should transition to HALF_OPEN."""
        if self._state == CircuitState.OPEN:
            now = self._clock()
            if now >= self._reset_at:
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                self._canary_in_flight = False

    def _compute_cooldown(self) -> float:
        """Calculate exponential backoff cooldown with +/- 20% jitter."""
        # 1st trip: base_cooldown * 2^0 = base_cooldown (30 s)
        # 2nd trip: base_cooldown * 2^1 = 60 s
        # 3rd trip: base_cooldown * 2^2 = 120 s
        # ... capped at max_cooldown_s (300 s)
        backoff_mult = 2 ** max(0, self._trip_count - 1)
        raw_cooldown = min(self.base_cooldown_s * backoff_mult, self.max_cooldown_s)

        # rng() returns [0.0, 1.0). Map to [-jitter_ratio, +jitter_ratio]
        # jitter_fraction = (rng * 2 - 1) * jitter_ratio
        rand_val = self._rng()
        jitter_fraction = (rand_val * 2.0 - 1.0) * self.jitter_ratio
        actual_cooldown = raw_cooldown * (1.0 + jitter_fraction)
        return max(0.0, actual_cooldown)

    def _trip(self) -> None:
        """Trip the breaker to OPEN."""
        self._state = CircuitState.OPEN
        self._trip_count += 1
        self._opened_at = self._clock()
        self._cooldown_s = self._compute_cooldown()
        self._reset_at = self._opened_at + self._cooldown_s
        self._canary_in_flight = False
        self._consecutive_successes = 0

    def record_success(self) -> None:
        """Record a successful dependency call."""
        with self._lock:
            self._check_state_transition()
            if self._state == CircuitState.HALF_OPEN:
                self._canary_in_flight = False
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    self._trip_count = 0
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    def record_failure(self, error: Optional[Exception] = None) -> None:
        """Record a failed dependency call."""
        with self._lock:
            self._check_state_transition()
            if self._state == CircuitState.HALF_OPEN:
                self._canary_in_flight = False
                self._trip()
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._trip()

    def reset(self) -> None:
        """Explicitly reset breaker to pristine CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._trip_count = 0
            self._opened_at = 0.0
            self._cooldown_s = 0.0
            self._reset_at = 0.0
            self._canary_in_flight = False

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute func through the circuit breaker."""
        with self._lock:
            self._check_state_transition()
            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Remaining cooldown: {max(0.0, self._reset_at - self._clock()):.2f}s"
                )
            if self._state == CircuitState.HALF_OPEN:
                if self._canary_in_flight:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker '{self.name}' is HALF_OPEN and canary probe is already in-flight."
                    )
                self._canary_in_flight = True

        # Execute outside lock to avoid blocking other threads during I/O
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self.record_failure(exc)
            raise
        else:
            self.record_success()
            return result

    def call_with_timeout(
        self,
        func: Callable[..., T],
        timeout_s: float,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute func through the circuit breaker enforcing a strict latency timeout budget."""
        return self.call(run_with_timeout, func, timeout_s, *args, **kwargs)


def run_with_timeout(
    func: Callable[..., T],
    timeout_s: float,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Execute callable enforcing strict latency timeout budget (REQ-AGENT-012)."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise ServiceTimeoutError(
            f"Execution exceeded latency budget of {timeout_s * 1000.0:.1f} ms"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
