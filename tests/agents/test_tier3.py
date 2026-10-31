"""Targeted unit tests for ticket_router.agents.triage_router.tier3 pure logic and invoker.

# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds.
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies.
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ticket_router.agents.triage_router import tier3
from ticket_router.models.router_s1_client import RouterPrediction


def test_tier3_config_resolution(monkeypatch: pytest.MonkeyPatch):
    """Verify Tier3Config resolution from environment overrides."""
    monkeypatch.setenv("TIER3_ENABLED", "0")
    monkeypatch.setenv("TIER3_CONFIDENCE_THRESHOLD", "0.85")
    monkeypatch.setenv("TIER3_DEADLINE_S", "4.0")

    cfg = tier3.Tier3Config.from_config()
    assert cfg.enabled is False
    assert cfg.confidence_threshold == 0.85
    assert cfg.deadline_s == 4.0


def test_tier3_trigger_evaluation():
    """Verify is_tier3_trigger across escalation, sentiment, urgency, and confidence."""
    # 1. Escalation / angry / immediate never trigger Tier 3 (add-only invariant)
    esc_pred = RouterPrediction(
        escalate=True,
        escalate_prob=0.9,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(esc_pred) == (False, False)

    angry_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="angry",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(angry_pred) == (False, False)

    imm_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="immediate",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.9,
    )
    assert tier3.is_tier3_trigger(imm_pred) == (False, False)

    # 2. Low confidence triggers Tier 3
    low_conf_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.1,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.60,  # < 0.70 threshold
    )
    should_trig, is_low = tier3.is_tier3_trigger(low_conf_pred, confidence_threshold=0.70)
    assert should_trig is True
    assert is_low is True

    # 3. High confidence benign does NOT trigger
    high_conf_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
    )
    assert tier3.is_tier3_trigger(high_conf_pred, confidence_threshold=0.70) == (False, False)

    # 4. Explicit multihop signal in raw metadata triggers Tier 3
    signal_pred = RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
        raw={"needs_multihop": True},
    )
    assert tier3.is_tier3_trigger(signal_pred, confidence_threshold=0.70) == (True, False)


def test_tier3_invoker_creation():
    """Verify create_tier3_invoker factory returns callable and handles custom caller."""
    custom_called = False

    def custom_invoker(text: str) -> RouterPrediction:
        nonlocal custom_called
        custom_called = True
        return RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent="order_status",
            intent_prob=0.95,
            latency_ms=10.0,
        )

    invoker = tier3.create_tier3_invoker(custom_caller=custom_invoker)
    pred = invoker("test message")
    assert custom_called is True
    assert pred.intent == "order_status"


def test_tier3_trigger_additional_branches():
    """Verify is_tier3_trigger additional branch coverage."""
    # 1. Low confidence on escalation certainty: (1.0 - escalate_prob) < threshold
    t2_pred_esc_uncert = RouterPrediction(
        escalate=False,
        escalate_prob=0.45,  # 1.0 - 0.45 = 0.55 < 0.70
        sentiment="neutral",
        sentiment_prob=0.9,
        urgency="normal",
        urgency_prob=0.9,
        intent="order_status",
        intent_prob=0.95,
    )
    trig, is_low = tier3.is_tier3_trigger(t2_pred_esc_uncert, confidence_threshold=0.70)
    assert trig is True
    assert is_low is True

    # 2. Metadata multihop signals
    for meta in (
        {"ambiguous": True},
        {"ambiguous": "yes"},
        {"ambiguous": "1"},
        {"needs_multihop": "true"},
        {"needs_multihop": "yes"},
        {"signal": "needs_multihop"},
        {"signal": "multihop"},
        {"reason": "ambiguous"},
    ):
        pred = RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent="order_status",
            intent_prob=0.95,
            raw=meta,
        )
        t, _ = tier3.is_tier3_trigger(pred, confidence_threshold=0.70)
        assert t is True, f"Failed for metadata {meta}"

    # 3. Intent multihop triggers
    for special_intent in ("needs_multihop", "ambiguous", "multihop"):
        pred_intent = RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.9,
            urgency="normal",
            urgency_prob=0.9,
            intent=special_intent,
            intent_prob=0.95,
        )
        t_int, _ = tier3.is_tier3_trigger(pred_intent, confidence_threshold=0.70)
        assert t_int is True, f"Failed for intent {special_intent}"


def test_invoke_tier3_gemini_mocked(monkeypatch: pytest.MonkeyPatch):
    """Verify invoke_tier3_gemini execution, hedged calling, markdown stripping, and usage parsing."""
    import google.genai

    mock_resp = MagicMock()
    mock_resp.text = (
        '```json\n{"escalate": false, "intent": "billing", "sentiment": "neutral", '
        '"urgency": "normal", "out_of_scope": false, "escalate_prob": 0.08, '
        '"intent_prob": 0.92}\n```'
    )
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 120
    mock_usage.candidates_token_count = 45
    mock_usage.total_token_count = 165
    mock_resp.usage_metadata = mock_usage

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    monkeypatch.setattr(google.genai, "Client", lambda **kwargs: mock_client)
    monkeypatch.setattr(tier3, "hedged_call", lambda fn, **kwargs: fn())

    pred = tier3.invoke_tier3_gemini(
        text="I need help with my invoice",
        target_model="gemini-2.5-pro",
        project="test-project",
        location="us-central1",
        deadline_s=3.0,
    )

    assert pred.intent == "billing"
    assert pred.escalate is False
    assert pred.intent_prob == 0.92
    assert pred.raw["usage"]["total_tokens"] == 165

    # Also test Tier3GeminiInvoker and default_tier3_invoker
    invoker = tier3.Tier3GeminiInvoker(project="test-project", location="us-central1", deadline_s=2.0)
    pred_inv = invoker("invoice query")
    assert pred_inv.intent == "billing"

    pred_def = tier3.default_tier3_invoker("invoice query")
    assert pred_def.intent == "billing"


def test_invoke_tier3_gemini_fallbacks_and_exceptions(monkeypatch: pytest.MonkeyPatch):
    """Verify invoke_tier3_gemini out_of_scope, non-JSON fallbacks, and error wrapping."""
    import google.genai

    mock_resp = MagicMock()
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    monkeypatch.setattr(google.genai, "Client", lambda **kwargs: mock_client)
    monkeypatch.setattr(tier3, "hedged_call", lambda fn, **kwargs: fn())

    # Case 1: Out of scope JSON
    mock_resp.text = '{"out_of_scope": true, "intent": "random"}'
    mock_resp.usage_metadata = None
    pred_oos = tier3.invoke_tier3_gemini("Tell me a joke", target_model="gemini-2.5-pro")
    assert pred_oos.out_of_scope is True
    assert pred_oos.intent == "other"

    # Case 2: String escalate and angry sentiment
    mock_resp.text = '{"escalate": "yes", "sentiment": "angry", "urgency": "immediate"}'
    pred_esc = tier3.invoke_tier3_gemini("Very upset customer", target_model="gemini-2.5-pro")
    assert pred_esc.escalate is True
    assert pred_esc.sentiment == "angry"
    assert pred_esc.urgency == "immediate"

    # Case 3: Non-JSON raw text fallback (angry keyword)
    mock_resp.text = "Customer is ANGRY and needs human representative"
    pred_raw = tier3.invoke_tier3_gemini("Angry text", target_model="gemini-2.5-pro")
    assert pred_raw.escalate is True
    assert pred_raw.sentiment == "angry"

    # Case 4: Exception wrapping
    mock_client.models.generate_content.side_effect = RuntimeError("Vertex API error")
    with pytest.raises(RuntimeError, match="Vertex API error"):
        tier3.invoke_tier3_gemini("Error query", target_model="gemini-2.5-pro")
