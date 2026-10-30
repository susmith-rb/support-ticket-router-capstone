"""Tests for hedged Gemini calls, router thinking cap, and deadline fallbacks.

# REQ-AGENT-025: Gemini Call Deadlines, Hedged Requests, and Router Thinking Cap
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""

from __future__ import annotations

import contextlib
import contextvars
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from ticket_router.agents.faq_resolver.agent import (
    FaqResolverAgent,
    GeminiAnswerClient,
)
from ticket_router.agents.triage_router.agent import invoke_tier2_gemini
from ticket_router.agents.triage_router.policy import TriagePolicy
from ticket_router.common import hedging as hedging_mod
from ticket_router.common.hedging import hedged_call
from ticket_router.common.observability import (
    NoOpSpan,
    get_current_traceparent,
    start_span,
)
from ticket_router.common.resilience import (
    ANSWER_DEADLINE_S,
    ANSWER_HEDGE_AFTER_S,
    TICKET_DB_MCP_TIMEOUT_S,
    TIER2_DEADLINE_S,
    TIER2_FLASH_TIMEOUT_S,
    TIER2_HEDGE_AFTER_S,
    ServiceTimeoutError,
)
from ticket_router.config.models import get, load_registry, model_thinking_level
from ticket_router.retrieval.search_client import SearchResult


@contextlib.contextmanager
def _capture_spans(monkeypatch: pytest.MonkeyPatch):
    """Capture spans created via ticket_router.common.hedging.start_span."""
    recorded: list[NoOpSpan] = []

    @contextlib.contextmanager
    def _fake_start_span(name: str, attributes: dict[str, Any] | None = None, traceparent: str | None = None):
        with start_span(name, attributes=attributes, traceparent=traceparent) as real_span:
            probe = NoOpSpan(name=name, attributes=dict(attributes or {}), traceparent=getattr(real_span, "traceparent", None))

            orig_set = real_span.set_attribute

            def _tee_attr(k: str, v: Any) -> None:
                probe.set_attribute(k, v)
                orig_set(k, v)

            monkeypatch.setattr(real_span, "set_attribute", _tee_attr)
            recorded.append(probe)
            yield real_span

    monkeypatch.setattr(hedging_mod, "start_span", _fake_start_span)
    yield recorded


# REQ-AGENT-025, REQ-AGENT-012, REQ-AGENT-029
def test_resilience_hedge_and_timeout_constants():
    """Verify REQ-AGENT-025 / REQ-AGENT-012 / REQ-AGENT-029 timeout and hedge constants."""
    assert TIER2_HEDGE_AFTER_S == 3.5
    assert TIER2_DEADLINE_S == 9.0
    assert ANSWER_HEDGE_AFTER_S == 4.0
    assert ANSWER_DEADLINE_S == 15.0
    assert TICKET_DB_MCP_TIMEOUT_S == 3.0
    # Superseded legacy constant remains exported for backward compatibility
    assert TIER2_FLASH_TIMEOUT_S == 1.5


# REQ-AGENT-025
def test_models_yaml_router_tier2_thinking_level_low():
    """Verify router_tier2 configures thinking_level LOW in models.yaml."""
    load_registry(reload=True)
    cfg = get("router_tier2")
    assert cfg.get("thinking_level") == "LOW"
    assert model_thinking_level("router_tier2") == "LOW"


# REQ-AGENT-025
def test_hedged_call_fast_path_no_hedge(monkeypatch: pytest.MonkeyPatch):
    """Fast primary attempt returns immediately without starting a hedge attempt."""
    calls = 0
    lock = threading.Lock()
    custom_var: contextvars.ContextVar[str] = contextvars.ContextVar("custom_var", default="unset")
    seen_vars: list[tuple[str, str | None]] = []

    def fast_fn() -> str:
        nonlocal calls
        with lock:
            calls += 1
        seen_vars.append((custom_var.get(), get_current_traceparent()))
        return "fast-ok"

    tok = custom_var.set("propagated-val")
    try:
        with _capture_spans(monkeypatch) as spans:
            with start_span("parent.turn"):
                res = hedged_call(
                    fast_fn,
                    hedge_after_s=0.15,
                    deadline_s=0.40,
                    span_name="test.fast",
                )
    finally:
        custom_var.reset(tok)

    assert res == "fast-ok"
    assert calls == 1
    assert seen_vars[0][0] == "propagated-val"
    assert seen_vars[0][1] is not None
    assert len(spans) == 1
    assert spans[0].attributes == {
        "hedge.attempts": 1,
        "hedge.winner": 1,
        "hedge.timed_out": False,
    }


# REQ-AGENT-025
def test_hedged_gemini_calls_bound_tail_latency(monkeypatch: pytest.MonkeyPatch):
    """Acceptance test: slow primary triggers hedge after hedge_after_s; hedge wins without waiting on hung primary."""
    attempt_num = 0
    lock = threading.Lock()
    release_hung = threading.Event()

    def bimodal_fn() -> str:
        nonlocal attempt_num
        with lock:
            attempt_num += 1
            my_attempt = attempt_num
        if my_attempt == 1:
            # Simulate bimodal tail stall well past deadline_s; must be abandoned on return
            release_hung.wait(timeout=2.0)
            return "stale-attempt-1"
        time.sleep(0.015)
        return "fast-hedge-attempt-2"

    try:
        with _capture_spans(monkeypatch) as spans:
            t0 = time.monotonic()
            result = hedged_call(
                bimodal_fn,
                hedge_after_s=0.05,
                deadline_s=0.45,
                span_name="test.hedge_wins",
            )
            elapsed = time.monotonic() - t0
    finally:
        release_hung.set()

    assert result == "fast-hedge-attempt-2"
    assert attempt_num == 2
    assert 0.045 <= elapsed < 0.30, f"Expected ~0.065s hedged completion, got {elapsed:.3f}s"
    assert len(spans) == 1
    assert spans[0].attributes == {
        "hedge.attempts": 2,
        "hedge.winner": 2,
        "hedge.timed_out": False,
    }


# REQ-AGENT-025
def test_hedged_call_first_raises_starts_hedge_immediately(monkeypatch: pytest.MonkeyPatch):
    """If attempt 1 raises before hedge_after_s, attempt 2 starts immediately without waiting hedge_after_s."""
    calls = 0
    lock = threading.Lock()

    def flaky_fn() -> str:
        nonlocal calls
        with lock:
            calls += 1
            curr = calls
        if curr == 1:
            raise ConnectionError("transient reset")
        return "recovered-immediately"

    with _capture_spans(monkeypatch) as spans:
        t0 = time.monotonic()
        # hedge_after_s is 5.0s; immediate retry on failure must finish in <0.2s
        res = hedged_call(
            flaky_fn,
            hedge_after_s=5.0,
            deadline_s=6.0,
            span_name="test.immediate_hedge",
        )
        elapsed = time.monotonic() - t0

    assert res == "recovered-immediately"
    assert calls == 2
    assert elapsed < 0.25, f"Hedge waited instead of starting immediately: {elapsed:.3f}s"
    assert spans[0].attributes == {
        "hedge.attempts": 2,
        "hedge.winner": 2,
        "hedge.timed_out": False,
    }


# REQ-AGENT-025
def test_hedged_call_both_fail_reraises_last_exception(monkeypatch: pytest.MonkeyPatch):
    """When all attempts fail with exceptions, hedged_call re-raises the last exception."""
    calls = 0
    lock = threading.Lock()

    def always_failing_fn() -> str:
        nonlocal calls
        with lock:
            calls += 1
            curr = calls
        if curr == 1:
            raise ValueError("first failure")
        raise KeyError("second failure")

    with _capture_spans(monkeypatch) as spans:
        with pytest.raises(KeyError, match="second failure"):
            hedged_call(
                always_failing_fn,
                hedge_after_s=2.0,
                deadline_s=3.0,
                span_name="test.both_fail",
            )

    assert calls == 2
    assert spans[0].attributes == {
        "hedge.attempts": 2,
        "hedge.winner": 0,
        "hedge.timed_out": False,
    }


# REQ-AGENT-025
def test_hedged_call_deadline_raises_service_timeout_error(monkeypatch: pytest.MonkeyPatch):
    """When no attempt succeeds within deadline_s, hedged_call raises ServiceTimeoutError without blocking."""
    release_hung = threading.Event()
    calls = 0
    lock = threading.Lock()

    def hung_fn() -> str:
        nonlocal calls
        with lock:
            calls += 1
        release_hung.wait(timeout=2.0)
        return "too-late"

    try:
        with _capture_spans(monkeypatch) as spans:
            t0 = time.monotonic()
            with pytest.raises(ServiceTimeoutError):
                hedged_call(
                    hung_fn,
                    hedge_after_s=0.04,
                    deadline_s=0.11,
                    span_name="test.deadline",
                )
            elapsed = time.monotonic() - t0
    finally:
        release_hung.set()

    assert calls == 2
    assert elapsed < 0.35, f"Deadline did not abandon hung threads promptly: {elapsed:.3f}s"
    assert spans[0].attributes == {
        "hedge.attempts": 2,
        "hedge.winner": 0,
        "hedge.timed_out": True,
    }


# REQ-AGENT-025
def test_invoke_tier2_gemini_passes_low_thinking_and_hedges(monkeypatch: pytest.MonkeyPatch):
    """Verify invoke_tier2_gemini passes ThinkingConfig(thinking_level='LOW'), uses hedged_call, and degrades on timeout."""
    load_registry(reload=True)
    recorded_calls: list[dict[str, Any]] = []
    hedged_invocations: list[dict[str, Any]] = []

    class FakeThinkingConfig:
        def __init__(self, *, thinking_level: str) -> None:
            self.thinking_level = thinking_level

    class FakeGenerateContentConfig:
        def __init__(self, *, system_instruction: str | None = None, thinking_config: Any = None) -> None:
            self.system_instruction = system_instruction
            self.thinking_config = thinking_config

    class FakeGenaiClient:
        def __init__(self, **kwargs: Any) -> None:
            self.models = SimpleNamespace(generate_content=self._generate_content)

        def _generate_content(self, model: str, contents: Any, **kwargs: Any) -> Any:
            recorded_calls.append({"model": model, "contents": contents, "kwargs": kwargs})
            return SimpleNamespace(
                text='{"escalate": false, "escalate_prob": 0.05, "sentiment": "neutral", "urgency": "normal", "intent": "shipping_policy", "intent_prob": 0.92}'
            )

    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeGenerateContentConfig,
        ThinkingConfig=FakeThinkingConfig,
    )
    fake_genai = SimpleNamespace(Client=FakeGenaiClient, types=fake_types)
    fake_google = SimpleNamespace(genai=fake_genai)
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    real_hedged = hedged_call

    def spy_hedged_call(fn, hedge_after_s, deadline_s, max_attempts=2, span_name=None, **kw):
        hedged_invocations.append({
            "hedge_after_s": hedge_after_s,
            "deadline_s": deadline_s,
            "span_name": span_name,
        })
        return real_hedged(fn, hedge_after_s, deadline_s, max_attempts=max_attempts, span_name=span_name, **kw)

    monkeypatch.setattr("ticket_router.agents.triage_router.agent.hedged_call", spy_hedged_call)

    pred = invoke_tier2_gemini("Where is my package?", target_model="test-router-flash")
    assert pred.escalate is False
    assert pred.intent == "shipping_policy"

    assert len(hedged_invocations) == 1
    assert hedged_invocations[0] == {
        "hedge_after_s": TIER2_HEDGE_AFTER_S,
        "deadline_s": TIER2_DEADLINE_S,
        "span_name": "tier2.generate_content",
    }

    assert len(recorded_calls) == 1
    cfg = recorded_calls[0]["kwargs"]["config"]
    assert isinstance(cfg, FakeGenerateContentConfig)
    assert cfg.system_instruction is not None
    assert isinstance(cfg.thinking_config, FakeThinkingConfig)
    assert cfg.thinking_config.thinking_level == "LOW"

    # Verify timeout in hedged_call propagates as RuntimeError -> policy degraded fallback
    def timeout_hedged_call(fn, hedge_after_s, deadline_s, max_attempts=2, span_name=None, **kw):
        raise ServiceTimeoutError("tier2 deadline exceeded")

    monkeypatch.setattr("ticket_router.agents.triage_router.agent.hedged_call", timeout_hedged_call)
    with pytest.raises(RuntimeError, match="Tier 2 model call failed"):
        invoke_tier2_gemini("Where is my package?", target_model="test-router-flash")

    failing_s1 = SimpleNamespace(route=lambda _t: (_ for _ in ()).throw(RuntimeError("s1 down")))
    policy = TriagePolicy(
        s1_client=failing_s1,  # type: ignore[arg-type]
        tier2_invoker=lambda text: invoke_tier2_gemini(text, target_model="test-router-flash"),
    )
    decision = policy.evaluate("Where is my package?", session_id="s-deg", customer_id="anon:s-deg")
    assert decision.decision == "escalate"
    assert decision.tier == "fallback_degraded"
    assert decision.degraded is True
    assert decision.queue == "human-standard"

    # Verify dict fallback path when google.genai.types lacks GenerateContentConfig, and omission when thinking_level is None
    monkeypatch.setattr("ticket_router.agents.triage_router.agent.hedged_call", real_hedged)
    empty_types = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "google.genai.types", empty_types)
    fake_genai.types = empty_types
    recorded_calls.clear()

    invoke_tier2_gemini("Where is my package?", target_model="test-router-flash")
    dict_cfg = recorded_calls[-1]["kwargs"]["config"]
    assert isinstance(dict_cfg, dict)
    assert dict_cfg["thinking_config"] == {"thinking_level": "LOW"}
    assert "system_instruction" in dict_cfg

    monkeypatch.setattr("ticket_router.agents.triage_router.agent.model_thinking_level", lambda _role: None)
    recorded_calls.clear()
    invoke_tier2_gemini("Where is my package?", target_model="test-router-flash")
    dict_cfg_no_think = recorded_calls[-1]["kwargs"]["config"]
    assert isinstance(dict_cfg_no_think, dict)
    assert "thinking_config" not in dict_cfg_no_think
    assert "system_instruction" in dict_cfg_no_think


# REQ-AGENT-025, REQ-AGENT-008
def test_gemini_answer_client_hedging_and_faq_resolver_timeout_escalation(monkeypatch: pytest.MonkeyPatch):
    """Verify GeminiAnswerClient wraps generate_content (and TypeError fallback) in hedged_call, and FaqResolverAgent escalates on LLM exception."""
    # CI installs only the [dev] extra (no google-genai), so inject a minimal google.genai.types
    # stub to make GeminiAnswerClient build a config and exercise the TypeError fallback path.
    import types as _pytypes

    fake_types = _pytypes.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kw: dict(kw)
    fake_types.ThinkingConfig = lambda **kw: dict(kw)
    fake_genai = _pytypes.ModuleType("google.genai")
    fake_genai.types = fake_types
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    hedged_calls: list[dict[str, Any]] = []
    call_count = 0

    class TypeErrorThenOkModels:
        def generate_content(self, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if "config" in kwargs:
                raise TypeError("unexpected keyword argument 'config'")
            return SimpleNamespace(
                text='{"answer": "Returns allowed within 30 days.", "citations": [{"doc_id": "d1", "quote": "within 30 days"}]}'
            )

    ans_client = GeminiAnswerClient(project="test-proj", location="us-central1", model_role="answer")
    ans_client._client = SimpleNamespace(models=TypeErrorThenOkModels())

    real_hedged = hedged_call

    def spy_hedged(fn, hedge_after_s, deadline_s, max_attempts=2, span_name=None, **kw):
        hedged_calls.append({
            "hedge_after_s": hedge_after_s,
            "deadline_s": deadline_s,
            "span_name": span_name,
        })
        return real_hedged(fn, hedge_after_s, deadline_s, max_attempts=max_attempts, span_name=span_name, **kw)

    monkeypatch.setattr("ticket_router.agents.faq_resolver.agent.hedged_call", spy_hedged)

    out = ans_client.generate_answer(
        query="What is the return policy?",
        retrieved_docs=[{"id": "d1", "title": "Returns", "content": "Returns allowed within 30 days."}],
    )
    assert out["answer"] == "Returns allowed within 30 days."
    assert len(hedged_calls) == 1
    assert hedged_calls[0] == {
        "hedge_after_s": ANSWER_HEDGE_AFTER_S,
        "deadline_s": ANSWER_DEADLINE_S,
        "span_name": "faq_resolver.generate_content",
    }
    assert call_count == 2  # 1st raised TypeError on config, fallback succeeded inside same hedged attempt

    # Now verify FaqResolverAgent escalates cleanly (never returns ungrounded answer) when generate_answer raises
    class TimeoutLLMClient:
        def __init__(self) -> None:
            self.calls = 0

        def generate_answer(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise ServiceTimeoutError("Hedged call exceeded deadline of 15000.0 ms")

    class StaticSearchClient:
        def search(self, **kwargs: Any) -> SearchResult:
            return SearchResult(
                document_ids=["d1"],
                latency_ms=10.0,
                total_size=1,
                filter_applied=None,
                fallback_triggered=False,
                rerank_applied=False,
                raw_results=[
                    {
                        "document": {
                            "id": "d1",
                            "derivedStructData": {"title": "Returns"},
                            "structData": {"content": "Returns allowed within 30 days."},
                        }
                    }
                ],
                summary={},
            )

    timeout_llm = TimeoutLLMClient()
    faq_agent = FaqResolverAgent(
        search_client=StaticSearchClient(),
        llm_client=timeout_llm,  # type: ignore[arg-type]
        reranker=None,
    )
    res = faq_agent.resolve(
        ticket_id="TICK-LLM-TIMEOUT",
        session_id="sess-llm-timeout",
        message="What is the return policy?",
        customer_id="anon:sess-llm-timeout",
        customer_verified=False,
        predicted_intent="returns_refunds",
    )
    # REQ-AGENT-028: Grounded static fallback on generation timeout when doc present
    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert res.degraded is True
    assert res.reason == "static_fallback"
    assert res.answer is not None
    assert "Returns allowed within 30 days." in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["doc_id"] == "d1"
    assert timeout_llm.calls == 1  # Does not waste another 15s regenerating after a hard timeout
