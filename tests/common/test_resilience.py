"""Tests for distributed resilience and latency budgets.

# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""

import time

import pytest

from ticket_router.common.resilience import (
    FAQ_OVERALL_BUDGET_S,
    ROUTER_S1_TIMEOUT_S,
    TIER2_FLASH_TIMEOUT_S,
    TRIAGE_OVERALL_BUDGET_S,
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    ServiceTimeoutError,
    run_with_timeout,
)


class MockClock:
    def __init__(self, start_time: float = 1000.0) -> None:
        self.current_time = start_time

    def now(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class MockRng:
    def __init__(self, fixed_val: float = 0.5) -> None:
        # fixed_val = 0.5 maps to 0 jitter: (0.5 * 2 - 1) = 0
        self.fixed_val = fixed_val

    def rand(self) -> float:
        return self.fixed_val


def _fail() -> None:
    raise RuntimeError("dependency outage")


def _should_not_run() -> str:
    return "unexpected"


def _slow_task() -> str:
    time.sleep(0.300)
    return "done"


def test_circuit_breakers_and_fallbacks():
    # REQ-AGENT-011: Circuit breaker state machine and fallback test
    clock = MockClock(start_time=1000.0)
    rng = MockRng(fixed_val=0.5)  # 0 jitter for predictable timing

    cb = CircuitBreaker(
        name="test-s1",
        failure_threshold=3,
        success_threshold=2,
        base_cooldown_s=30.0,
        max_cooldown_s=300.0,
        jitter_ratio=0.20,
        clock=clock.now,
        rng=rng.rand,
    )

    assert cb.state == CircuitState.CLOSED

    # 1. Normal successes keep it CLOSED
    res = cb.call(lambda: "ok")
    assert res == "ok"
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0

    # 2. 1st and 2nd failures keep it CLOSED
    with pytest.raises(RuntimeError):
        cb.call(_fail)
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 1

    with pytest.raises(RuntimeError):
        cb.call(_fail)
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 2

    # 3. 3rd consecutive failure trips breaker to OPEN
    with pytest.raises(RuntimeError):
        cb.call(_fail)
    assert cb.state == CircuitState.OPEN
    assert cb.trip_count == 1
    # Trip 1 cooldown = 30.0 s
    assert cb.reset_at == 1030.0

    # 4. Immediate calls while OPEN raise CircuitBreakerOpenError without invoking target
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.call(_should_not_run)
    assert "OPEN" in str(exc_info.value)

    # 5. Advance clock past 30s base cooldown -> transitions to HALF_OPEN
    clock.advance(30.1)
    assert cb.state == CircuitState.HALF_OPEN

    # 6. In HALF_OPEN: first canary success
    res_canary1 = cb.call(lambda: "canary_1")
    assert res_canary1 == "canary_1"
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.consecutive_successes == 1

    # 7. In HALF_OPEN: second canary success transitions back to CLOSED
    res_canary2 = cb.call(lambda: "canary_2")
    assert res_canary2 == "canary_2"
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert cb.consecutive_successes == 0

    # 8. Test exponential backoff on subsequent trips
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(_fail)
    assert cb.state == CircuitState.OPEN
    assert cb.trip_count == 1  # reset to 1 on fresh trip
    assert cb.reset_at == clock.now() + 30.0

    # Advance to HALF_OPEN and fail the canary -> immediate trip 2 (60 s cooldown)
    clock.advance(30.1)
    assert cb.state == CircuitState.HALF_OPEN
    with pytest.raises(RuntimeError):
        cb.call(_fail)
    assert cb.state == CircuitState.OPEN
    assert cb.trip_count == 2
    # Base 30 * 2^1 = 60.0 s
    assert cb.reset_at == clock.now() + 60.0

    # 9. Test fallback hierarchy pattern
    # When breaker is open, fallback to Tier 2 Flash is invoked
    try:
        triage_res = cb.call(lambda: "primary-s1")
    except CircuitBreakerOpenError:
        triage_res = "fallback-tier2-flash"

    # Breaker is currently OPEN
    assert triage_res == "fallback-tier2-flash"

    # 10. Test jitter range
    # rng = 0.0 -> jitter = -20% -> 30 * 0.8 = 24.0 s
    jitter_low_cb = CircuitBreaker(
        base_cooldown_s=30.0,
        clock=clock.now,
        rng=lambda: 0.0,
    )
    for _ in range(3):
        with pytest.raises(RuntimeError):
            jitter_low_cb.call(_fail)
    assert jitter_low_cb.state == CircuitState.OPEN
    assert abs(jitter_low_cb.reset_at - (clock.now() + 24.0)) < 1e-4

    # rng = 1.0 -> jitter = +20% -> 30 * 1.2 = 36.0 s
    jitter_high_cb = CircuitBreaker(
        base_cooldown_s=30.0,
        clock=clock.now,
        rng=lambda: 1.0,
    )
    for _ in range(3):
        with pytest.raises(RuntimeError):
            jitter_high_cb.call(_fail)
    assert jitter_high_cb.state == CircuitState.OPEN
    assert abs(jitter_high_cb.reset_at - (clock.now() + 36.0)) < 1e-4


def test_latency_budgets_and_timeouts():
    # REQ-AGENT-012: Execution budgets and service timeouts
    # Verify budget constants match specification
    # HTTP call timeout (not the 150 ms model budget): e2e :rawPredict p95 ~189 ms in-region
    assert ROUTER_S1_TIMEOUT_S == 0.500
    assert TIER2_FLASH_TIMEOUT_S == 1.500
    assert TRIAGE_OVERALL_BUDGET_S == 3.000
    assert FAQ_OVERALL_BUDGET_S == 20.000

    # Fast operation completes well within budget
    result = run_with_timeout(lambda x: x * 2, timeout_s=0.200, x=21)
    assert result == 42

    # Slow operation exceeding budget raises ServiceTimeoutError without waiting for full task sleep
    t0 = time.monotonic()
    with pytest.raises(ServiceTimeoutError) as exc_info:
        run_with_timeout(_slow_task, timeout_s=0.040)
    elapsed = time.monotonic() - t0
    assert "exceeded latency budget" in str(exc_info.value)
    assert elapsed < 0.200, f"run_with_timeout blocked on hung thread for {elapsed:.3f}s"

    # CircuitBreaker.call_with_timeout records successes and timeout failures
    cb = CircuitBreaker(name="timeout-cb", failure_threshold=2)
    assert cb.call_with_timeout(lambda v: v + 1, 0.200, 41) == 42
    assert cb.consecutive_failures == 0

    with pytest.raises(ServiceTimeoutError):
        cb.call_with_timeout(_slow_task, timeout_s=0.030)
    assert cb.consecutive_failures == 1
    assert cb.state == CircuitState.CLOSED

    with pytest.raises(ServiceTimeoutError):
        cb.call_with_timeout(_slow_task, timeout_s=0.030)
    assert cb.state == CircuitState.OPEN


def test_circuit_breaker_properties_and_cooldown():
    """Verify CircuitBreaker properties, state transitions, and cooldown jitter.

    # REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
    # REQ-CICD-010: Targeted tests for common/resilience.py pure logic
    """
    fake_time = 100.0

    def clock():
        return fake_time

    # RNG returning 0.5 means jitter_fraction = (0.5 * 2 - 1) * 0.20 = 0.0
    cb = CircuitBreaker(
        name="test_cb",
        failure_threshold=2,
        base_cooldown_s=10.0,
        max_cooldown_s=60.0,
        jitter_ratio=0.20,
        clock=clock,
        rng=lambda: 0.5,
    )

    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert cb.consecutive_successes == 0
    assert cb.trip_count == 0
    assert cb.reset_at == 0.0

    # First failure
    cb.record_failure(RuntimeError("fail 1"))
    assert cb.consecutive_failures == 1
    assert cb.state == CircuitState.CLOSED

    # Second failure trips breaker
    cb.record_failure(RuntimeError("fail 2"))
    assert cb.state == CircuitState.OPEN
    assert cb.trip_count == 1
    assert cb.reset_at == 110.0  # 100.0 + 10.0

    # Before cooldown expires
    fake_time = 105.0
    assert cb.state == CircuitState.OPEN

    # After cooldown expires, state transitions to HALF_OPEN on read
    fake_time = 111.0
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.consecutive_successes == 0

    # Success in HALF_OPEN recovers breaker to CLOSED (success_threshold=2)
    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
