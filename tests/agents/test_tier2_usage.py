"""Unit tests for Tier 2 Gemini usage and model ID capture in RouterPrediction.raw.

# REQ-REVAL-011: Runner targets, tier cache, dry-run & cost cap (Tier 2 usage capture)
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, Optional

import pytest

from ticket_router.agents.triage_router.agent import invoke_tier2_gemini


class FakeUsageMetadata:
    def __init__(
        self,
        prompt_token_count: Optional[int] = 42,
        candidates_token_count: Optional[int] = 18,
        total_token_count: Optional[int] = 60,
    ) -> None:
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count
        self.total_token_count = total_token_count


class FakeResponseWithUsage:
    def __init__(
        self,
        text: str = (
            '{"escalate": false, "sentiment": "neutral", "urgency": "normal", '
            '"intent": "faq", "escalate_prob": 0.10, "intent_prob": 0.90}'
        ),
        usage_metadata: Optional[Any] = None,
    ) -> None:
        self.text = text
        self.usage_metadata = usage_metadata


@pytest.fixture
def fake_genai_with_usage(monkeypatch: pytest.MonkeyPatch):
    """Inject hermetic fake google.genai module with configurable usage_metadata."""
    state: Dict[str, Any] = {
        "text": (
            '{"escalate": false, "sentiment": "neutral", "urgency": "normal", '
            '"intent": "faq", "escalate_prob": 0.10, "intent_prob": 0.90}'
        ),
        "usage_metadata": FakeUsageMetadata(42, 18, 60),
    }

    class FakeModels:
        def generate_content(self, model: str, contents: Any, **kwargs: Any) -> FakeResponseWithUsage:
            return FakeResponseWithUsage(
                text=state["text"],
                usage_metadata=state["usage_metadata"],
            )

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.models = FakeModels()

    fake_module = types.ModuleType("google.genai")
    fake_module.Client = FakeClient  # type: ignore[attr-defined]

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_module)

    return state


def test_tier2_usage_captured_in_router_prediction_raw(fake_genai_with_usage: Dict[str, Any]):
    """REQ-REVAL-011: invoke_tier2_gemini attaches usage and model_id to RouterPrediction.raw."""
    pred = invoke_tier2_gemini(
        text="How do I return my order?",
        target_model="gemini-3.8-flash-preview",
    )
    assert pred.intent == "faq"
    assert pred.raw["model_id"] == "gemini-3.8-flash-preview"
    assert pred.raw["usage"] == {
        "prompt_tokens": 42,
        "output_tokens": 18,
        "total_tokens": 60,
    }


def test_tier2_usage_missing_or_none(fake_genai_with_usage: Dict[str, Any]):
    """REQ-REVAL-011: invoke_tier2_gemini handles missing usage_metadata gracefully."""
    fake_genai_with_usage["usage_metadata"] = None
    pred = invoke_tier2_gemini(
        text="How do I return my order?",
        target_model="gemini-3.8-flash-preview",
    )
    assert pred.intent == "faq"
    assert pred.raw["model_id"] == "gemini-3.8-flash-preview"
    assert pred.raw["usage"] is None


def test_tier2_usage_captured_on_fallback_text_match(fake_genai_with_usage: Dict[str, Any]):
    """REQ-REVAL-011: Fallback text matching still captures usage and model_id."""
    fake_genai_with_usage["text"] = "Non-json text: I am angry and need help immediately!"
    pred = invoke_tier2_gemini(
        text="I am angry and need help immediately!",
        target_model="gemini-3.8-flash-preview",
    )
    assert pred.escalate is True
    assert pred.sentiment == "angry"
    assert pred.raw["model_id"] == "gemini-3.8-flash-preview"
    assert pred.raw["usage"] == {
        "prompt_tokens": 42,
        "output_tokens": 18,
        "total_tokens": 60,
    }
    assert "raw_text" in pred.raw
