"""Model Armor client budgets: egress timeout and single failure accounting."""

# REQ-MA-005

from ticket_router.common.resilience import CircuitBreaker
from ticket_router.security.model_armor import (
    DEFAULT_EGRESS_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    ModelArmorClient,
)

TEMPLATE = "projects/p/locations/us-central1/templates/t"


def test_egress_uses_longer_budget_than_ingress(monkeypatch):
    # REQ-MA-005
    seen = []
    client = ModelArmorClient(template=TEMPLATE, token_provider=lambda: "tok")

    def fake_call(rpc_name, request_body, timeout_s=None):
        seen.append((rpc_name, timeout_s))
        return None

    monkeypatch.setattr(client, "_call_api", fake_call)
    client.sanitize_user_prompt("hi")
    client.sanitize_model_response("long answer")
    assert seen[0] == ("sanitizeUserPrompt", None)  # falls back to ingress budget
    assert seen[1] == ("sanitizeModelResponse", DEFAULT_EGRESS_TIMEOUT_S)
    assert DEFAULT_EGRESS_TIMEOUT_S > DEFAULT_TIMEOUT_S


def test_one_failed_call_counts_once_against_breaker():
    # REQ-MA-005
    breaker = CircuitBreaker(name="ma_test", failure_threshold=3, base_cooldown_s=30.0)

    def boom():
        raise RuntimeError("token fetch failed")

    client = ModelArmorClient(template=TEMPLATE, token_provider=boom, circuit_breaker=breaker)
    v = client.sanitize_user_prompt("hello")
    assert v.degraded is True
    assert breaker._consecutive_failures == 1
    client.sanitize_user_prompt("hello again")
    assert breaker._consecutive_failures == 2
    assert breaker.state.value != "open"
