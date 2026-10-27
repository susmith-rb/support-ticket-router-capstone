"""Unit and contract tests for TriageRouterAgent and tiered routing policy.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
"""

from unittest.mock import MagicMock

from ticket_router.agents.triage_router.a2a_client import (
    A2AHandoffPayload,
    HttpA2AClient,
    InMemoryA2AClient,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import (
    InMemoryTicketDbMcpClient,
    compute_idempotency_key,
)
from ticket_router.agents.triage_router.policy import (
    QueuePriority,
    RouterThresholds,
    Tier0Result,
    TriagePolicy,
    VerifiedFacts,
    evaluate_gaming_resistant_priority,
    evaluate_tier0_rules,
)
from ticket_router.agents.triage_router.templates import render_calming_acknowledgement
from ticket_router.common.resilience import CircuitBreakerOpenError
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client


def _make_prediction(
    escalate: bool = False,
    escalate_prob: float = 0.05,
    sentiment: str = "neutral",
    sentiment_prob: float = 0.90,
    urgency: str = "normal",
    urgency_prob: float = 0.90,
    intent: str = "shipping",
    intent_prob: float = 0.85,
    latency_ms: float = 35.0,
) -> RouterPrediction:
    return RouterPrediction(
        escalate=escalate,
        escalate_prob=escalate_prob,
        sentiment=sentiment,
        sentiment_prob=sentiment_prob,
        urgency=urgency,
        urgency_prob=urgency_prob,
        intent=intent,
        intent_prob=intent_prob,
        latency_ms=latency_ms,
    )


# -----------------------------------------------------------------------------
# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# -----------------------------------------------------------------------------

def test_tiered_policy_thresholds():
    # REQ-AGENT-002: Test boundary value conditions at each threshold
    thresholds = RouterThresholds(tau_esc=0.35, tau_low=0.15, tau_intent=0.70)

    # 1. Tier 0 Rule triggers: Emergency keyword
    t0_emergency = evaluate_tier0_rules("Call the police, there is an emergency.")
    assert t0_emergency.matched is True
    assert t0_emergency.escalate is True
    assert t0_emergency.category == "emergency"

    # Human agent request trigger
    t0_human = evaluate_tier0_rules("I want to speak to a human representative please.")
    assert t0_human.matched is True
    assert t0_human.escalate is True
    assert t0_human.category == "human_request"

    # 2. Tier 1 Router-S1: Definitive escalation at boundary p_esc >= 0.35
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_tier2 = MagicMock()

    policy = TriagePolicy(thresholds=thresholds, s1_client=mock_s1, tier2_invoker=mock_tier2)

    # Exactly at tau_esc (0.35) -> Auto-escalate, Tier 2 must NOT be called
    mock_s1.route.return_value = _make_prediction(escalate_prob=0.35, sentiment="neutral", urgency="normal")
    decision = policy.evaluate("Standard delay query", session_id="s1", customer_id="c1")
    assert decision.decision == "escalate"
    assert decision.tier == "tier1_router_s1"
    assert not mock_tier2.called

    # Well above tau_esc (0.80) -> Auto-escalate
    mock_s1.route.return_value = _make_prediction(escalate_prob=0.80, sentiment="angry")
    decision = policy.evaluate("Very bad experience", session_id="s1", customer_id="c1")
    assert decision.decision == "escalate"
    assert decision.tier == "tier1_router_s1"
    assert not mock_tier2.called

    # 3. Tier 1 Router-S1: Definitive auto-RAG at p_esc < 0.15 and p_intent >= 0.70
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.149,
        intent="shipping",
        intent_prob=0.70,
        sentiment="neutral",
        urgency="normal",
    )
    decision = policy.evaluate("Where is my package?", session_id="s1", customer_id="c1")
    assert decision.decision == "auto_rag"
    assert decision.tier == "tier1_router_s1"
    assert decision.predicted_intent == "shipping"
    assert not mock_tier2.called

    # 4. Uncertainty Band 1: tau_low <= p_esc < tau_esc (0.15 <= p_esc < 0.35)
    # Exactly at tau_low boundary (0.15) -> Triggers Tier 2 Flash
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.15,
        intent="shipping",
        intent_prob=0.85,
    )
    mock_tier2.return_value = _make_prediction(escalate=False, intent="shipping")
    decision = policy.evaluate("Boundary inquiry", session_id="s1", customer_id="c1")
    assert mock_tier2.called
    assert decision.tier == "tier2_flash"

    # Uncertainty Band 2: Just below tau_esc (0.349) -> Triggers Tier 2 Flash
    mock_tier2.reset_mock()
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.349,
        intent="shipping",
        intent_prob=0.85,
    )
    mock_tier2.return_value = _make_prediction(escalate=True, sentiment="neutral")
    decision = policy.evaluate("Upper boundary inquiry", session_id="s1", customer_id="c1")
    assert mock_tier2.called
    assert decision.tier == "tier2_flash"
    assert decision.decision == "escalate"

    # Uncertainty Band 3: Low p_esc (< 0.15) but intent confidence < tau_intent (0.70)
    mock_tier2.reset_mock()
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.08,
        intent="shipping",
        intent_prob=0.699,  # Just below 0.70
    )
    mock_tier2.return_value = _make_prediction(escalate=False, intent="billing")
    decision = policy.evaluate("Vague inquiry", session_id="s1", customer_id="c1")
    assert mock_tier2.called
    assert decision.tier == "tier2_flash"
    assert decision.predicted_intent == "billing"

    # Uncertainty Band 4: intent == 'other' -> Triggers Tier 2 Flash
    mock_tier2.reset_mock()
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.05,
        intent="other",
        intent_prob=0.90,
    )
    mock_tier2.return_value = _make_prediction(escalate=False, intent="account")
    decision = policy.evaluate("Unclassified inquiry", session_id="s1", customer_id="c1")
    assert mock_tier2.called
    assert decision.tier == "tier2_flash"


# -----------------------------------------------------------------------------
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# -----------------------------------------------------------------------------

def test_add_only_escalation_invariant():
    # REQ-AGENT-003: final.escalate = rules OR tier1 OR tier2
    thresholds = RouterThresholds()

    # 1. Tier 0 escalates: downstream models cannot clear escalation
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_tier2 = MagicMock()
    policy = TriagePolicy(thresholds=thresholds, s1_client=mock_s1, tier2_invoker=mock_tier2)

    decision = policy.evaluate("I need a lawyer right now for a lawsuit!", session_id="s1", customer_id="c1")
    assert decision.escalate is True
    assert decision.decision == "escalate"
    assert decision.tier == "tier0_rules"
    # Neither s1 nor tier2 were even allowed to de-escalate
    assert not mock_s1.route.called
    assert not mock_tier2.called

    # 2. Tier 1 escalates: downstream Tier 2 cannot clear escalation
    mock_s1.route.return_value = _make_prediction(
        escalate=True,
        escalate_prob=0.85,
        sentiment="angry",
    )
    decision = policy.evaluate("Angry customer complaint", session_id="s1", customer_id="c1")
    assert decision.escalate is True
    assert decision.decision == "escalate"
    assert decision.tier == "tier1_router_s1"
    assert not mock_tier2.called

    # 3. In uncertainty band: Tier 1 marked angry, Tier 2 attempts to de-escalate
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.25,
        sentiment="angry",  # Tier 1 flagged angry
    )
    # Tier 2 attempts to claim escalate=False and neutral
    mock_tier2.return_value = _make_prediction(
        escalate=False,
        sentiment="neutral",
        urgency="normal",
    )
    decision = policy.evaluate("Frustrated note", session_id="s1", customer_id="c1")
    # Invariant: final.escalate MUST remain True!
    assert decision.escalate is True
    assert decision.decision == "escalate"

    # 4. Abuse / Toxic phrase handling: MUST escalate to human, NEVER dropped or suppressed
    toxic_texts = [
        "You fucking scammers, where is my package?",
        "This service is complete shit, you idiots!",
        "Go die in a hole",
    ]
    for toxic in toxic_texts:
        t0 = evaluate_tier0_rules(toxic)
        assert t0.matched is True
        assert t0.escalate is True
        assert t0.category == "abuse"

        dec = policy.evaluate(toxic, session_id="s1", customer_id="c1")
        # Ticket is escalated to human, NOT dropped
        assert dec.escalate is True
        assert dec.decision == "escalate"
        assert dec.queue in ("human-standard", "human-urgent")


# -----------------------------------------------------------------------------
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# -----------------------------------------------------------------------------

def test_gaming_resistant_priority():
    # REQ-AGENT-004: Anger/abuse alone does NOT inflate priority; verified facts do

    # Scenario A: Furious customer shouting profanities, but NO verified urgency facts
    # (standard order, no delay, non-VIP)
    t0_abuse = Tier0Result(matched=True, escalate=True, category="abuse", sentiment="angry")
    low_facts = VerifiedFacts(order_value=25.0, delivery_delay_hours=0.0, vip_customer=False)
    q_abuse = evaluate_gaming_resistant_priority(facts=low_facts, tier0=t0_abuse)
    # MUST be Standard (P2), anger alone cannot game Urgent (P1)
    assert q_abuse == QueuePriority.P2_STANDARD
    assert q_abuse.value == "human-standard"

    # Scenario B: Calm, polite inquiry with VERIFIED 48h delivery delay (>24h SLA)
    delay_facts = VerifiedFacts(delivery_delay_hours=48.0, order_value=30.0, vip_customer=False)
    q_delay = evaluate_gaming_resistant_priority(facts=delay_facts, tier0=None, sentiment="neutral")
    # Verified business delay triggers P1 Urgent
    assert q_delay == QueuePriority.P1_URGENT
    assert q_delay.value == "human-urgent"

    # Scenario C: Calm inquiry with high monetary transaction value ($250 >= $100 threshold)
    high_val_facts = VerifiedFacts(order_value=250.0)
    q_val = evaluate_gaming_resistant_priority(facts=high_val_facts)
    assert q_val == QueuePriority.P1_URGENT

    # Scenario D: VIP account status or stranded return
    vip_facts = VerifiedFacts(vip_customer=True)
    assert evaluate_gaming_resistant_priority(facts=vip_facts) == QueuePriority.P1_URGENT

    stranded_facts = VerifiedFacts(stranded_return=True)
    assert evaluate_gaming_resistant_priority(facts=stranded_facts) == QueuePriority.P1_URGENT

    # Scenario E: Angry customer AND verified delay
    # Priority is P1 Urgent because of the delay, not the anger
    q_both = evaluate_gaming_resistant_priority(facts=delay_facts, tier0=t0_abuse)
    assert q_both == QueuePriority.P1_URGENT


# -----------------------------------------------------------------------------
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# -----------------------------------------------------------------------------

def test_calming_ack_and_idempotent_upsert():
    # REQ-AGENT-005: Calming ack and idempotent ticket persistence

    # 1. Verify calming ack contents
    ack_urgent = render_calming_acknowledgement(queue="human-urgent", ticket_id="TICK-1001")
    assert "TICK-1001" in ack_urgent
    assert "15 minutes" in ack_urgent

    ack_standard = render_calming_acknowledgement(queue="human-standard", ticket_id="TICK-1002")
    assert "TICK-1002" in ack_standard
    assert "2 hours" in ack_standard

    ack_abuse = render_calming_acknowledgement(queue="human-standard", ticket_id="TICK-1003", category="abuse")
    assert "TICK-1003" in ack_abuse
    assert "respectful environment" in ack_abuse
    assert "2 hours" in ack_abuse

    # 2. Verify deterministic idempotency key derivation (session_id:message_hash)
    key1 = compute_idempotency_key("sess_abc", "Where is order 123?")
    key2 = compute_idempotency_key("sess_abc", "Where is order 123?")
    key3 = compute_idempotency_key("sess_abc", "Different message text")
    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("sess_abc:")

    # 3. Test end-to-end agent idempotency with InMemoryTicketDbMcpClient
    mcp_client = InMemoryTicketDbMcpClient()
    a2a_client = InMemoryA2AClient()
    agent = TriageRouterAgent(mcp_client=mcp_client, a2a_client=a2a_client)

    # First turn creates ticket
    res1 = agent.triage(
        session_id="sess_idemp",
        customer_id="cust_1",
        message_text="I demand to speak to a representative!",
        ticket_id="TICK-FIXED-1",
    )
    assert res1.decision.decision == "escalate"
    assert res1.calming_ack is not None
    assert len(mcp_client.tickets) == 1

    # Retried identical request
    res2 = agent.triage(
        session_id="sess_idemp",
        customer_id="cust_1",
        message_text="I demand to speak to a representative!",
        ticket_id="TICK-FIXED-1",
    )
    # Store must deduplicate and NOT create a second ticket row
    assert len(mcp_client.tickets) == 1
    assert res1.idempotency_key == res2.idempotency_key


# -----------------------------------------------------------------------------
# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# -----------------------------------------------------------------------------

def test_a2a_handoff_protocol():
    # REQ-AGENT-006: Handoff of non-escalated factual inquiries to FaqResolverAgent
    mock_s1 = MagicMock(spec=RouterS1Client)
    # Definitive auto-RAG prediction
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.04,
        intent="shipping",
        intent_prob=0.92,
        sentiment="neutral",
    )

    policy = TriagePolicy(s1_client=mock_s1)
    mcp_client = InMemoryTicketDbMcpClient()
    a2a_client = InMemoryA2AClient()

    agent = TriageRouterAgent(policy=policy, mcp_client=mcp_client, a2a_client=a2a_client)

    result = agent.triage(
        session_id="sess_a2a",
        customer_id="cust_42",
        message_text="What is your standard shipping delivery window?",
        ticket_id="TICK-A2A-1",
        memory_summary="Customer preferred expedited carrier in past orders.",
    )

    assert result.decision.decision == "auto_rag"
    assert result.calming_ack is None  # No escalation calming ack
    assert result.handoff_response is not None
    assert result.handoff_response["status"] == "ACCEPTED"

    # Verify A2A handoff payload contents
    assert len(a2a_client.handoffs) == 1
    payload: A2AHandoffPayload = a2a_client.handoffs[0]
    assert payload.ticket_id == "TICK-A2A-1"
    assert payload.session_id == "sess_a2a"
    assert payload.predicted_intent == "shipping"
    assert "shipping delivery window" in payload.redacted_message
    assert "expedited carrier" in payload.memory_summary

    # Verify HTTP client construction and headers
    mock_http_session = MagicMock()
    mock_http_session.post.return_value.status_code = 200
    mock_http_session.post.return_value.json.return_value = {"status": "OK"}

    http_a2a = HttpA2AClient(
        endpoint_url="https://faq-resolver.internal.net",
        target_audience="sa-faq@susmith-fde-ticket-router.iam.gserviceaccount.com",
        token_provider=lambda aud: "mock_oidc_token",
        session=mock_http_session,
    )
    http_resp = http_a2a.handoff(payload)
    assert http_resp["status"] == "OK"

    post_call = mock_http_session.post.call_args
    assert post_call is not None
    assert post_call.args[0] == "https://faq-resolver.internal.net/a2a/handoff"
    assert post_call.kwargs["headers"]["Authorization"] == "Bearer mock_oidc_token"


# -----------------------------------------------------------------------------
# REQ-AGENT-011: Circuit Breaker Fallback in Triage Policy
# -----------------------------------------------------------------------------

def test_triage_circuit_breaker_fallback():
    # REQ-AGENT-011: When router-s1 breaker is OPEN, policy falls back to Tier 2 Flash
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.side_effect = CircuitBreakerOpenError("router-s1 breaker OPEN")

    mock_tier2 = MagicMock()
    mock_tier2.return_value = _make_prediction(
        escalate=False,
        intent="billing",
        intent_prob=0.88,
    )

    policy = TriagePolicy(s1_client=mock_s1, tier2_invoker=mock_tier2)
    decision = policy.evaluate("How do I update my payment card?", session_id="s1", customer_id="c1")

    assert mock_tier2.called
    assert decision.tier == "tier2_flash"
    assert decision.degraded is True
    assert decision.decision == "auto_rag"
    assert decision.predicted_intent == "billing"
