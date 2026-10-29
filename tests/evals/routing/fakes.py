"""Hermetic fake clients for routing evaluation tests.

# REQ-REVAL-011 / REQ-REVAL-015 / REQ-REVAL-018
Dependency-free fakes for RouterS1Client, Tier 2 Gemini Flash invoker,
and Model Armor client. Shared across WP-C and WP-D test suites.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ticket_router.config.models import model_id as resolve_model_id
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.model_armor import FilterVerdict, ModelArmorVerdict


def make_prediction(
    intent: str = "order_status",
    decision: str = "auto_rag",
    escalate: bool = False,
    escalate_prob: float = 0.05,
    intent_prob: float = 0.90,
    sentiment: str = "neutral",
    urgency: str = "normal",
    latency_ms: float = 25.0,
    usage: Optional[Dict[str, int]] = None,
    model_id: str = "test-model",
    raw_extra: Optional[Dict[str, Any]] = None,
) -> RouterPrediction:
    """Helper to build a strongly-typed RouterPrediction for testing."""
    raw = dict(raw_extra or {})
    raw["model_id"] = model_id
    if usage is not None:
        raw["usage"] = usage

    return RouterPrediction(
        escalate=escalate,
        escalate_prob=escalate_prob,
        sentiment=sentiment,
        sentiment_prob=0.90,
        urgency=urgency,
        urgency_prob=0.90,
        intent=intent,
        intent_prob=intent_prob,
        probabilities={intent: {intent: intent_prob}},
        latency_ms=latency_ms,
        raw=raw,
    )


class FakeS1Client:
    """Deterministic fake client for Tier 1 router-s1 encoder."""

    def __init__(
        self,
        default_pred: Optional[RouterPrediction] = None,
        responses: Optional[Dict[str, RouterPrediction]] = None,
        delay_s: float = 0.0,
        error_on_text: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self.default_pred = default_pred or make_prediction(
            intent="order_status",
            escalate=False,
            escalate_prob=0.08,
            intent_prob=0.92,
            latency_ms=18.0,
            model_id="router-s1",
        )
        self.responses = responses or {}
        self.delay_s = delay_s
        self.error_on_text = error_on_text or {}
        self.call_history: List[str] = []

    def route(self, text: str) -> RouterPrediction:
        self.call_history.append(text)
        if self.delay_s > 0:
            time.sleep(self.delay_s)

        if text in self.error_on_text:
            raise self.error_on_text[text]

        if text in self.responses:
            return self.responses[text]

        # Keyed on substring or return default
        for k, v in self.responses.items():
            if k in text:
                return v

        return self.default_pred


class FakeTier2Invoker:
    """Deterministic fake callable for Tier 2 Gemini Flash fallback."""

    def __init__(
        self,
        default_pred: Optional[RouterPrediction] = None,
        responses: Optional[Dict[str, RouterPrediction]] = None,
        pass_responses: Optional[Dict[int, RouterPrediction]] = None,
        delay_s: float = 0.0,
        error_on_text: Optional[Dict[str, Exception]] = None,
        usage: Optional[Dict[str, int]] = None,
        model_id: Optional[str] = None,
    ) -> None:
        eff_model_id = model_id or resolve_model_id("router_tier2")
        eff_usage = usage or {"prompt_tokens": 280, "output_tokens": 45, "total_tokens": 325}
        self.default_pred = default_pred or make_prediction(
            intent="returns_refunds",
            escalate=False,
            escalate_prob=0.12,
            intent_prob=0.88,
            latency_ms=95.0,
            usage=eff_usage,
            model_id=eff_model_id,
        )
        self.responses = responses or {}
        self.pass_responses = pass_responses or {}
        self.delay_s = delay_s
        self.error_on_text = error_on_text or {}
        self.model_id = eff_model_id
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, text: str, pass_index: int = 0) -> RouterPrediction:
        self.calls.append({"text": text, "pass_index": pass_index})
        if self.delay_s > 0:
            time.sleep(self.delay_s)

        if text in self.error_on_text:
            raise self.error_on_text[text]

        if pass_index in self.pass_responses:
            return self.pass_responses[pass_index]

        if text in self.responses:
            return self.responses[text]

        for k, v in self.responses.items():
            if k in text:
                return v

        return self.default_pred


class FakeModelArmorClient:
    """Deterministic fake Model Armor client."""

    def __init__(
        self,
        default_verdict: Optional[ModelArmorVerdict] = None,
        blocked_texts: Optional[List[str]] = None,
    ) -> None:
        self.default_verdict = default_verdict or ModelArmorVerdict(
            is_clean=True,
            blocked=False,
            action="ALLOW",
            sanitized_text=None,
            latency_ms=8.0,
        )
        self.blocked_texts = blocked_texts or []
        self.calls: List[str] = []

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        self.calls.append(text)
        if any(b in text for b in self.blocked_texts):
            return ModelArmorVerdict(
                is_clean=False,
                blocked=True,
                action="BLOCK",
                pi_jb=FilterVerdict(tripped=True, confidence="HIGH"),
                latency_ms=12.0,
            )
        return self.default_verdict


@dataclass(frozen=True)
class FakeRoutingCase:
    """Minimal routing case compatible with evals.routing.schema.RoutingCase."""

    ticket_id: str
    description: str
    subject: Optional[str] = None
    split: str = "dev"
    scenarios: List[str] = field(default_factory=lambda: ["nominal"])

    @property
    def full_text(self) -> str:
        return f"{self.subject}\n\n{self.description}" if self.subject else self.description
