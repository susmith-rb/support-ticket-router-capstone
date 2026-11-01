"""Latency budget, concurrent SLA, and short-circuit tests for Model Armor.

# REQ-MA-005: In-Region Latency Budget, Concurrent SLA & Degraded Metrics
"""

import time
from unittest.mock import MagicMock

from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    TriagePolicy,
    VerifiedFacts,
)
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.model_armor import (
    FakeModelArmorClient,
    ModelArmorVerdict,
)


def test_latency_budget_and_short_circuit():
    """Verify concurrent execution overlap, no Tier 0 keyword short-circuit, and degraded telemetry."""
    # 1. Concurrent Execution Overlap (Model Armor + router-s1)
    # When both operations take 80ms, concurrent execution finishes in ~80-100ms (< 150ms).
    s1_delay_s = 0.08
    ma_delay_s = 0.08

    mock_s1 = MagicMock()

    def _slow_s1_route(text: str) -> RouterPrediction:
        time.sleep(s1_delay_s)
        return RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.10,
            urgency="normal",
            urgency_prob=0.10,
            intent="shipping_inquiry",
            intent_prob=0.90,
            latency_ms=s1_delay_s * 1000.0,
        )

    mock_s1.route.side_effect = _slow_s1_route

    class DelayedFakeModelArmor(FakeModelArmorClient):
        def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
            time.sleep(ma_delay_s)
            return super().sanitize_user_prompt(text)

    delayed_ma = DelayedFakeModelArmor()
    policy = TriagePolicy(s1_client=mock_s1, model_armor_client=delayed_ma)

    t0 = time.perf_counter()
    dec: RouterDecision = policy.evaluate(
        text="Where is my shipment?",
        session_id="sess-budget-001",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )
    elapsed_total_s = time.perf_counter() - t0

    # Overlapping execution check: Sequential would take >= 160ms.
    # Concurrent execution should complete well under 140ms.
    assert elapsed_total_s < (s1_delay_s + ma_delay_s) * 0.90
    assert dec.decision == "auto_rag"

    # 2. No Tier 0 keyword short-circuit (REQ-TURN-001): a legal threat is NOT decided by a
    # phrase rule; Model Armor still runs (PII tokenization) and router-s1 is consulted.
    mock_s1.reset_mock()
    mock_s1.route.side_effect = _slow_s1_route
    ma_fake = FakeModelArmorClient()
    policy_no_t0 = TriagePolicy(s1_client=mock_s1, model_armor_client=ma_fake)
    dec_legal = policy_no_t0.evaluate(
        text="I am going to sue you and take legal action immediately.",
        session_id="sess-t0-001",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )
    assert dec_legal.tier != "tier0_rules"
    mock_s1.route.assert_called_once()
    assert len(ma_fake.prompt_calls) == 1

    # 3. Degraded Telemetry Invariant on Timeout/Outage
    ma_outage = FakeModelArmorClient(fail_with_timeout=True)
    policy_outage = TriagePolicy(s1_client=mock_s1, model_armor_client=ma_outage)

    dec_outage = policy_outage.evaluate(
        text="Normal inquiry while Model Armor is degraded",
        session_id="sess-deg-001",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )
    assert dec_outage.degraded is True
    assert dec_outage.escalate is True
    assert dec_outage.reason == "model_armor_degraded"
    assert dec_outage.details.get("model_armor", {}).get("degraded") is True
