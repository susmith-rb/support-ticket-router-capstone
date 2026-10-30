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
    HttpTicketDbMcpClient,
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
from ticket_router.config.taxonomy import is_valid_intent
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.security.model_armor import FilterVerdict, ModelArmorVerdict


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

def test_all_policy_tiers_emit_canonical_taxonomy_intent():
    # REQ-AGENT-002: All policy tiers emit canonical taxonomy intent ("other" for off-taxonomy escalations).
    # REQ-REVAL-001: Intent validated against canonical taxonomy source of truth.
    policy = TriagePolicy(thresholds=RouterThresholds())

    # 1. Tier 0 rules: emergency keyword
    dec_emerg = policy.evaluate("Call the police, there is an emergency.", session_id="s_em", customer_id="c1")
    assert dec_emerg.tier == "tier0_rules"
    assert dec_emerg.decision == "escalate"
    assert dec_emerg.predicted_intent == "other"
    assert is_valid_intent(dec_emerg.predicted_intent)
    assert dec_emerg.details["category"] == "emergency"
    assert dec_emerg.reason == "tier0_emergency_trigger"

    # Tier 0 rules: abuse keyword
    dec_abuse = policy.evaluate("You are a stupid idiot and a moron", session_id="s_ab", customer_id="c1")
    assert dec_abuse.tier == "tier0_rules"
    assert dec_abuse.decision == "escalate"
    assert dec_abuse.predicted_intent == "other"
    assert is_valid_intent(dec_abuse.predicted_intent)
    assert dec_abuse.details["category"] == "abuse"
    assert dec_abuse.reason == "tier0_abusive_language"

    # Tier 0 rules: human agent request
    dec_human = policy.evaluate("I want to speak to a human representative please.", session_id="s_hu", customer_id="c1")
    assert dec_human.tier == "tier0_rules"
    assert dec_human.decision == "escalate"
    assert dec_human.predicted_intent == "other"
    assert is_valid_intent(dec_human.predicted_intent)
    assert dec_human.details["category"] == "human_request"
    assert dec_human.reason == "tier0_human_agent_request"

    # 2. Model Armor security trigger (PI/JB)
    mock_ma = MagicMock()
    mock_ma.sanitize_user_prompt.return_value = ModelArmorVerdict(
        is_clean=False,
        blocked=True,
        action="BLOCK",
        pi_jb=FilterVerdict(tripped=True, confidence="HIGH"),
        sanitized_text="clean text",
    )
    policy_ma = TriagePolicy(thresholds=RouterThresholds(), model_armor_client=mock_ma)
    dec_ma_sec = policy_ma.evaluate("Harmless inquiry", session_id="s_masec", customer_id="c1")
    assert dec_ma_sec.tier == "model_armor"
    assert dec_ma_sec.decision == "escalate"
    assert dec_ma_sec.predicted_intent == "other"
    assert is_valid_intent(dec_ma_sec.predicted_intent)
    assert dec_ma_sec.details["category"] == "security"
    assert dec_ma_sec.reason == "model_armor_security_trigger"

    # Model Armor RAI abuse trigger
    mock_ma_rai = MagicMock()
    mock_ma_rai.sanitize_user_prompt.return_value = ModelArmorVerdict(
        is_clean=False,
        blocked=True,
        action="BLOCK",
        rai_categories={"harassment": FilterVerdict(tripped=True, confidence="HIGH")},
        sanitized_text="clean text",
    )
    policy_ma_rai = TriagePolicy(thresholds=RouterThresholds(), model_armor_client=mock_ma_rai)
    dec_ma_rai = policy_ma_rai.evaluate("Harmless inquiry", session_id="s_marai", customer_id="c1")
    assert dec_ma_rai.tier == "model_armor"
    assert dec_ma_rai.decision == "escalate"
    assert dec_ma_rai.predicted_intent == "other"
    assert is_valid_intent(dec_ma_rai.predicted_intent)
    assert dec_ma_rai.details["category"] == "abuse"
    assert dec_ma_rai.reason == "model_armor_rai_trigger"

    # Model Armor degraded
    mock_ma_deg = MagicMock()
    mock_ma_deg.sanitize_user_prompt.return_value = ModelArmorVerdict(
        is_clean=False,
        blocked=False,
        action="DEGRADE",
        degraded=True,
    )
    policy_ma_deg = TriagePolicy(thresholds=RouterThresholds(), model_armor_client=mock_ma_deg)
    dec_ma_deg = policy_ma_deg.evaluate("Harmless inquiry", session_id="s_madeg", customer_id="c1")
    assert dec_ma_deg.tier == "model_armor"
    assert dec_ma_deg.decision == "escalate"
    assert dec_ma_deg.predicted_intent == "other"
    assert is_valid_intent(dec_ma_deg.predicted_intent)
    assert dec_ma_deg.details["category"] == "degraded"
    assert dec_ma_deg.reason == "model_armor_degraded"

    # 3. Fallback degraded (Tier 1 & Tier 2 unavailable)
    mock_s1_fail = MagicMock(spec=RouterS1Client)
    mock_s1_fail.route.side_effect = RuntimeError("S1 down")
    mock_t2_fail = MagicMock()
    mock_t2_fail.side_effect = RuntimeError("T2 down")
    policy_deg = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1_fail,
        tier2_invoker=mock_t2_fail,
    )
    dec_deg = policy_deg.evaluate("Where is my package?", session_id="s_deg", customer_id="c1")
    assert dec_deg.tier == "fallback_degraded"
    assert dec_deg.decision == "escalate"
    assert dec_deg.predicted_intent == "other"
    assert is_valid_intent(dec_deg.predicted_intent)
    assert "degraded_fallback" in dec_deg.reason
    assert "s1_error" in dec_deg.details


def test_tiered_policy_thresholds():
    # REQ-AGENT-002: Test boundary value conditions at each threshold
    assert RouterThresholds().tau_esc == 0.50
    assert RouterThresholds().tau_low == 0.15
    assert RouterThresholds().tau_intent == 0.70
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

    # 5. Configurable tau_esc > 0.50 (e.g. 0.60): S1 raw binary escalate=True does NOT auto-escalate
    # when p_esc < tau_esc; falls through to Tier 2 Flash (REQ-AGENT-002)
    thresholds_high = RouterThresholds(tau_esc=0.60, tau_low=0.15, tau_intent=0.70)
    mock_s1_high = MagicMock(spec=RouterS1Client)
    mock_tier2_high = MagicMock()
    policy_high = TriagePolicy(thresholds=thresholds_high, s1_client=mock_s1_high, tier2_invoker=mock_tier2_high)

    mock_s1_high.route.return_value = _make_prediction(
        escalate=True,
        escalate_prob=0.55,
        sentiment="neutral",
        urgency="normal",
        intent="order_status",
        intent_prob=0.85,
    )
    mock_tier2_high.return_value = _make_prediction(escalate=False, intent="order_status")
    decision_high = policy_high.evaluate("Order status check", session_id="s1", customer_id="c1")
    assert mock_tier2_high.called
    assert decision_high.decision == "auto_rag"
    assert decision_high.tier == "tier2_flash"


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


# =============================================================================
# Runtime Wiring & Fail-Fast Tests
# =============================================================================


class FakeMcpResponse:
    """Mock HTTP response for MCP client testing."""

    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} error")


def test_http_ticket_db_mcp_client_sends_bearer_token():
    """Verify HttpTicketDbMcpClient sends OIDC ID token in Authorization header."""
    mock_session = MagicMock()
    mock_session.post.return_value = FakeMcpResponse({"ticket_id": "t1", "status": "OPEN"})

    client = HttpTicketDbMcpClient(
        base_url="https://ticket-db.run.app",
        session=mock_session,
        token_provider=lambda aud: "mock_ticket_db_id_token",
    )
    client.claim_ticket("t1", "agent-007")

    assert mock_session.post.called
    call_kwargs = mock_session.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer mock_ticket_db_id_token"


def test_http_a2a_client_sends_customer_id_and_bearer():
    """Verify HttpA2AClient passes customer_id in payload and Authorization Bearer header."""
    mock_session = MagicMock()
    mock_session.post.return_value = FakeMcpResponse({"status": "ACCEPTED"})

    client = HttpA2AClient(
        endpoint_url="https://faq-resolver.run.app",
        target_audience="https://faq-resolver.run.app",
        session=mock_session,
        token_provider=lambda aud: "mock_a2a_id_token",
    )
    payload = A2AHandoffPayload(
        ticket_id="tick-1",
        session_id="sess-1",
        predicted_intent="shipping",
        redacted_message="Where is my box?",
        customer_id="cust-vip-100",
    )
    resp = client.handoff(payload)
    assert resp["status"] == "ACCEPTED"

    call_kwargs = mock_session.post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer mock_a2a_id_token"

    import json
    body_data = json.loads(call_kwargs["data"].decode("utf-8"))
    assert body_data["customer_id"] == "cust-vip-100"


def test_triage_router_lazy_init_fail_fast_on_missing_env(monkeypatch):
    """Verify TriageRouterAgent with lazy_init=True fails fast on first request if env is missing."""
    import pytest
    monkeypatch.setenv("REQUIRE_REAL_DEPS", "1")
    monkeypatch.delenv("TICKET_DB_MCP_URL", raising=False)
    monkeypatch.delenv("FAQ_RESOLVER_URL", raising=False)

    agent = TriageRouterAgent(lazy_init=True)
    with pytest.raises(ValueError, match="Missing required environment variable"):
        agent.triage(session_id="s1", customer_id="c1", message_text="hello")


def test_triage_router_root_agent_import_and_query_in_test_env():
    """Verify root_agent can be imported and queried safely in test environment."""
    from ticket_router.agents.triage_router.agent import root_agent
    assert root_agent is not None
    # In test environment (where require_real_deps is False), query succeeds with fakes
    res = root_agent.query("What is the return policy?", session_id="s_test", customer_id="c_test")
    assert "ticket_id" in res
    assert "decision" in res


def test_invoke_tier2_gemini_json_parsing_and_invoker_wiring(monkeypatch):
    """Verify invoke_tier2_gemini parses JSON escalate=false vs true accurately and create_tier2_invoker forwards project/location."""
    import pickle
    import sys
    from types import SimpleNamespace

    from ticket_router.agents.triage_router.agent import (
        create_tier2_invoker,
        invoke_tier2_gemini,
    )

    recorded_clients = []

    class FakeGenaiClient:
        def __init__(self, **kwargs):
            recorded_clients.append(kwargs)
            self.models = SimpleNamespace(generate_content=self._generate_content)

        def _generate_content(self, model, contents, **kwargs):
            if "i want a refund" in contents.lower():
                return SimpleNamespace(
                    text='```json\n{"escalate": true, "escalate_prob": 0.91, "sentiment": "angry", "urgency": "immediate", "intent": "returns_refunds", "intent_prob": 0.88}\n```'
                )
            return SimpleNamespace(
                text='{"escalate": false, "escalate_prob": 0.08, "sentiment": "neutral", "urgency": "normal", "intent": "shipping_policy", "intent_prob": 0.94}'
            )

    fake_google = SimpleNamespace(genai=SimpleNamespace(Client=FakeGenaiClient))
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_google.genai)

    # 1. JSON with escalate=false must NOT be treated as escalate=True
    pred_no_esc = invoke_tier2_gemini(
        "What are your shipping options?",
        target_model="gemini-test-model",
        project="proj-tier2",
        location="us-central1",
    )
    assert pred_no_esc.escalate is False
    assert pred_no_esc.escalate_prob == 0.08
    assert pred_no_esc.intent == "shipping_policy"
    assert pred_no_esc.intent_prob == 0.94
    assert recorded_clients[-1] == {"project": "proj-tier2", "vertexai": True, "location": "us-central1"}

    # 2. JSON with escalate=true in markdown fence
    pred_esc = invoke_tier2_gemini(
        "I want a refund right now!",
        target_model="gemini-test-model",
    )
    assert pred_esc.escalate is True
    assert pred_esc.escalate_prob == 0.91
    assert pred_esc.sentiment == "angry"
    assert pred_esc.urgency == "immediate"
    assert pred_esc.intent == "returns_refunds"

    # 3. create_tier2_invoker forwards project & location (with registry precedence) and is picklable
    invoker = create_tier2_invoker("router_tier2", project="proj-custom", location="us-west1")
    unpickled_invoker = pickle.loads(pickle.dumps(invoker))
    res = unpickled_invoker("What are your shipping options?")
    assert res.escalate is False
    assert res.intent == "shipping_policy"
    assert recorded_clients[-1] == {"project": "proj-custom", "vertexai": True, "location": "global"}


def test_triage_a2a_handoff_failure_degrades_to_escalation():
    """Verify TriageRouterAgent degrades gracefully to human escalation when A2A handoff raises or escalates."""
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.04,
        intent="shipping",
        intent_prob=0.92,
        sentiment="neutral",
    )
    policy = TriagePolicy(s1_client=mock_s1)
    mcp_client = InMemoryTicketDbMcpClient()

    failing_a2a = MagicMock()
    failing_a2a.handoff.side_effect = TimeoutError("Read timed out (read timeout=20.0)")

    agent = TriageRouterAgent(policy=policy, mcp_client=mcp_client, a2a_client=failing_a2a)
    result = agent.triage(
        session_id="sess_a2a_timeout",
        customer_id="cust_99",
        message_text="What is the status of order ORD-10001?",
        ticket_id="TICK-TIMEOUT-1",
    )

    assert result.decision.decision == "escalate"
    assert result.decision.escalate is True
    assert result.decision.degraded is True
    assert result.decision.queue == "human-standard"
    assert result.decision.details.get("a2a_error") == "TimeoutError"
    assert result.calming_ack is not None
    assert "TICK-TIMEOUT-1" in result.calming_ack
    assert result.handoff_response is not None
    assert result.handoff_response["status"] == "ESCALATED"
    assert result.handoff_response["reason"] == "a2a_handoff_failed"


def test_triage_double_publish_guard_with_faq_handoff():
    """Verify TriageRouter does not double-publish when FaqResolver completes HumanHandoff (REQ-AGENT-030)."""
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = _make_prediction(
        escalate_prob=0.04,
        intent="shipping",
        intent_prob=0.92,
        sentiment="neutral",
    )
    policy = TriagePolicy(s1_client=mock_s1)
    mcp_client = InMemoryTicketDbMcpClient()

    # Case 1: FaqResolver returns handoff_done=True
    mock_a2a_done = MagicMock()
    mock_a2a_done.handoff.return_value = {
        "status": "ESCALATED",
        "handoff_done": True,
        "queue": "human-standard",
        "reason": "circuit_breaker_open",
        "calming_ack": "Thank you for contacting support. Ticket #TICK-GUARD-1 is with human support (ETA 2 hours).",
    }
    mock_handoff1 = MagicMock()

    agent1 = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=mock_a2a_done,
        human_handoff=mock_handoff1,
    )
    res1 = agent1.triage(
        session_id="sess_guard_1",
        customer_id="cust_1",
        message_text="Need shipping info",
        ticket_id="TICK-GUARD-1",
    )
    assert res1.decision.decision == "auto_rag"
    assert res1.handoff_response["status"] == "ESCALATED"
    assert "TICK-GUARD-1" in res1.calming_ack
    # Crucial assertion: human_handoff.escalate was NOT called by TriageRouter
    mock_handoff1.escalate.assert_not_called()

    # Case 2: FaqResolver returns handoff_done=False (or missing)
    mock_a2a_not_done = MagicMock()
    mock_a2a_not_done.handoff.return_value = {
        "status": "ESCALATED",
        "handoff_done": False,
        "queue": "human-standard",
        "reason": "faq_escalated",
    }
    mock_handoff2 = MagicMock()
    mock_handoff2.escalate.return_value = MagicMock(
        calming_ack="Calming ack from triage fallback",
        persisted=True,
        published=True,
        message_id="msg-123",
    )

    agent2 = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=mock_a2a_not_done,
        human_handoff=mock_handoff2,
    )
    res2 = agent2.triage(
        session_id="sess_guard_2",
        customer_id="cust_2",
        message_text="Need shipping info",
        ticket_id="TICK-GUARD-2",
    )
    assert res2.decision.decision == "auto_rag"
    assert res2.handoff_response["status"] == "ESCALATED"
    assert res2.calming_ack == "Calming ack from triage fallback"
    # Crucial assertion: human_handoff.escalate WAS called by TriageRouter
    mock_handoff2.escalate.assert_called_once()




