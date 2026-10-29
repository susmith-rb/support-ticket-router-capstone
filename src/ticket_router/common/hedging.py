"""Hedged request execution with hard deadlines and OpenTelemetry span attributes.

# REQ-AGENT-025: Gemini Call Deadlines, Hedged Requests, and Router Thinking Cap
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import time
from typing import Any, Callable, TypeVar

from ticket_router.common.observability import start_span
from ticket_router.common.resilience import ServiceTimeoutError

T = TypeVar("T")


def hedged_call(
    fn: Callable[[], T],
    hedge_after_s: float,
    deadline_s: float,
    max_attempts: int = 2,
    span_name: str | None = None,
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Execute ``fn`` with hedged backup attempts and a hard deadline (REQ-AGENT-025).

    Starts attempt 1 immediately in a ThreadPoolExecutor. If attempt 1 has not
    completed after ``hedge_after_s`` (or raises an exception before the hedge
    point), starts the next attempt up to ``max_attempts``. Returns the first
    successful result. Re-raises the last exception if all attempts fail before
    ``deadline_s``; raises ``ServiceTimeoutError`` if no attempt succeeds by
    ``deadline_s``.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    _clock = clock if clock is not None else time.monotonic
    hedge_delay = max(0.0, float(hedge_after_s))
    budget = float(deadline_s)

    span_cm: contextlib.AbstractContextManager[Any] = (
        start_span(span_name) if span_name else contextlib.nullcontext(None)
    )

    with span_cm as span:
        attempts_started = 0
        winner = 0
        timed_out = False
        last_exc: Exception | None = None

        if budget <= 0.0:
            timed_out = True
            if span is not None:
                span.set_attribute("hedge.attempts", 0)
                span.set_attribute("hedge.winner", 0)
                span.set_attribute("hedge.timed_out", True)
            raise ServiceTimeoutError(
                f"Hedged call exceeded deadline of {budget * 1000.0:.1f} ms"
            )

        t0 = _clock()
        hard_deadline = t0 + budget
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_attempts)
        pending: set[concurrent.futures.Future[T]] = set()
        future_to_attempt: dict[concurrent.futures.Future[T], int] = {}
        next_hedge_at = t0 + hedge_delay

        def _launch_attempt() -> None:
            nonlocal attempts_started, next_hedge_at
            attempts_started += 1
            ctx = contextvars.copy_context()
            fut = executor.submit(ctx.run, fn)
            future_to_attempt[fut] = attempts_started
            pending.add(fut)
            next_hedge_at = _clock() + hedge_delay

        try:
            _launch_attempt()

            while True:
                done_now = [f for f in pending if f.done()]
                if done_now:
                    pending.difference_update(done_now)
                    failed_now = False
                    for fut in sorted(done_now, key=lambda f: future_to_attempt[f]):
                        try:
                            res = fut.result()
                        except Exception as exc:
                            last_exc = exc
                            failed_now = True
                        else:
                            winner = future_to_attempt[fut]
                            return res

                    if not pending and attempts_started >= max_attempts:
                        assert last_exc is not None
                        raise last_exc

                    if failed_now and attempts_started < max_attempts:
                        if _clock() >= hard_deadline:
                            timed_out = True
                            raise ServiceTimeoutError(
                                f"Hedged call exceeded deadline of {budget * 1000.0:.1f} ms"
                            )
                        _launch_attempt()
                        continue

                now = _clock()
                remaining_deadline = hard_deadline - now
                if remaining_deadline <= 0.0:
                    timed_out = True
                    raise ServiceTimeoutError(
                        f"Hedged call exceeded deadline of {budget * 1000.0:.1f} ms"
                    )

                if attempts_started < max_attempts:
                    until_hedge = next_hedge_at - now
                    if until_hedge <= 0.0:
                        _launch_attempt()
                        continue
                    wait_s = min(remaining_deadline, until_hedge)
                else:
                    if not pending:
                        if last_exc is not None:
                            raise last_exc
                        timed_out = True
                        raise ServiceTimeoutError(
                            f"Hedged call exceeded deadline of {budget * 1000.0:.1f} ms"
                        )
                    wait_s = remaining_deadline

                if sleep is not None:
                    sleep(wait_s)
                else:
                    concurrent.futures.wait(
                        pending,
                        timeout=wait_s,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
        finally:
            if span is not None:
                span.set_attribute("hedge.attempts", attempts_started)
                span.set_attribute("hedge.winner", winner)
                span.set_attribute("hedge.timed_out", timed_out)
            executor.shutdown(wait=False, cancel_futures=True)
