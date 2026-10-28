"""Hermetic unit tests for Tier 2 model location resolution and precedence.

# REQ-AGENT-013: Centralized Model Configuration Resolution
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List

import pytest

from ticket_router.agents.triage_router.agent import (
    Tier2GeminiInvoker,
    TriageRouterAgent,
    default_tier2_invoker,
)
from ticket_router.config.models import model_id, model_location


@pytest.fixture
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """Inject hermetic fake google.genai module without external SDK dependency."""
    calls: List[Dict[str, Any]] = []
    instances: List[Any] = []

    class FakeResponse:
        def __init__(
            self,
            text: str = (
                '{"escalate": false, "sentiment": "neutral", "urgency": "normal", '
                '"intent": "faq", "escalate_prob": 0.10, "intent_prob": 0.90}'
            ),
        ) -> None:
            self.text = text

    class FakeModels:
        def generate_content(self, model: str, contents: Any, **kwargs: Any) -> FakeResponse:
            calls.append({"model": model, "contents": contents, "kwargs": kwargs})
            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.init_kwargs = kwargs
            self.models = FakeModels()
            instances.append(self)

    fake_module = types.ModuleType("google.genai")
    fake_module.Client = FakeClient  # type: ignore[attr-defined]

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_module)

    return {"instances": instances, "calls": calls}


def test_model_location_unit_resolution():
    """REQ-AGENT-013 (d): Test model_location resolution, defaults, and fallbacks."""
    # Known roles in models.yaml with explicit locations
    assert model_location("router_tier2") == "global"
    assert model_location("answer") == "global"
    assert model_location("cross_family_gpt_oss") == "us-central1"

    # Role without configured location returns None or passed default
    assert model_location("agent_authorship_tag") is None
    assert model_location("agent_authorship_tag", default="us-central1") == "us-central1"

    # Non-existent role returns None or passed default
    assert model_location("nonexistent_role_xyz") is None
    assert model_location("nonexistent_role_xyz", default="europe-west4") == "europe-west4"


def test_tier2_invoker_location_precedence(fake_genai: Dict[str, List[Any]], monkeypatch: pytest.MonkeyPatch):
    """REQ-AGENT-013 (a): Registry location takes precedence over Settings.location / GOOGLE_CLOUD_LOCATION."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    expected_location = model_location("router_tier2")
    assert expected_location == "global"
    expected_model = model_id("router_tier2")

    # 1. Direct Tier2GeminiInvoker call with passed Settings-like location "us-central1"
    invoker = Tier2GeminiInvoker(
        model_role="router_tier2",
        project="test-project",
        location="us-central1",
    )
    pred = invoker("Where is my package?")
    assert pred.intent == "faq"

    client = fake_genai["instances"][-1]
    assert client.init_kwargs.get("location") == expected_location
    assert client.init_kwargs.get("project") == "test-project"
    assert client.init_kwargs.get("vertexai") is True
    assert fake_genai["calls"][-1]["model"] == expected_model

    # 2. Invoker created via REQUIRE_REAL_DEPS path in TriageRouterAgent
    monkeypatch.setenv("REQUIRE_REAL_DEPS", "true")
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("ROUTER_S1_ENDPOINT", "projects/123/locations/us-central1/endpoints/456")
    monkeypatch.setenv("ROUTER_S1_BASE_URL", "https://us-central1-aiplatform.googleapis.com")
    monkeypatch.setenv("FAQ_RESOLVER_URL", "https://faq-service.a.run.app")
    monkeypatch.setenv("TICKET_DB_MCP_URL", "https://ticket-db.a.run.app")
    monkeypatch.setenv("MODEL_ARMOR_ENABLED", "false")

    agent = TriageRouterAgent(lazy_init=False)
    assert agent.policy.tier2_invoker is not None

    agent.policy.tier2_invoker("Where is my package?")
    client_agent = fake_genai["instances"][-1]
    assert client_agent.init_kwargs.get("location") == expected_location
    assert client_agent.init_kwargs.get("project") == "test-project"
    assert client_agent.init_kwargs.get("vertexai") is True
    assert fake_genai["calls"][-1]["model"] == expected_model


def test_default_tier2_invoker_location_precedence(
    fake_genai: Dict[str, List[Any]], monkeypatch: pytest.MonkeyPatch
):
    """REQ-AGENT-013 (b): default_tier2_invoker resolves location and model from registry."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    expected_location = model_location("router_tier2")
    assert expected_location == "global"
    expected_model = model_id("router_tier2")

    pred = default_tier2_invoker("Where is my package?")
    assert pred.intent == "faq"

    client = fake_genai["instances"][-1]
    assert client.init_kwargs.get("location") == expected_location
    assert client.init_kwargs.get("project") == "test-project"
    assert client.init_kwargs.get("vertexai") is True
    assert fake_genai["calls"][-1]["model"] == expected_model


def test_role_without_location_fallback(fake_genai: Dict[str, List[Any]], monkeypatch: pytest.MonkeyPatch):
    """REQ-AGENT-013 (c): Role without configured location falls back to passed location."""
    role = "agent_authorship_tag"
    assert model_location(role) is None
    expected_model = model_id(role)

    invoker = Tier2GeminiInvoker(
        model_role=role,
        project="test-project",
        location="us-central1",
    )
    pred = invoker("Where is my package?")
    assert pred.intent == "faq"

    client = fake_genai["instances"][-1]
    assert client.init_kwargs.get("location") == "us-central1"
    assert client.init_kwargs.get("project") == "test-project"
    assert client.init_kwargs.get("vertexai") is True
    assert fake_genai["calls"][-1]["model"] == expected_model
