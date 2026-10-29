"""End-to-end tests for out-of-scope query refusal via Tier-2 system prompt.

# REQ-AGENT-022: Out-of-Scope Query Refusal via Tier-2 System Prompt
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    extract_answer_from_result,
)
from ticket_router.agents.triage_router.agent import (
    TriageRouterAgent,
    invoke_tier2_gemini,
)
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
)
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
)
from ticket_router.config.agent_tuning import (
    _CONTEXT_TIER2_VERSION,
    get_tier2_prompt,
    get_tier2_system_instruction,
)
from ticket_router.mcp.ticket_db.server import create_app
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client
from ticket_router.store.fake import FakeTicketRepo


def _make_prediction(
    escalate: bool = False,
    escalate_prob: float = 0.05,
    sentiment: str = "neutral",
    sentiment_prob: float = 0.90,
    urgency: str = "normal",
    urgency_prob: float = 0.90,
    intent: str = "other",
    intent_prob: float = 0.85,
    latency_ms: float = 35.0,
    out_of_scope: bool = False,
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
        out_of_scope=out_of_scope,
    )


class SpyA2AClient:
    """Spy implementation of A2A client recording all handoff calls."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def handoff(self, payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        return {"status": "RESOLVED_AUTO", "answer": "Mock automated answer"}


def test_out_of_scope_refusal_end_to_end():
    # REQ-AGENT-022: Calm unrelated inquiry refuses at scope boundary without human queue bloat.
    # REQ-AGENT-005: Idempotent upsert with resolved_auto status and assigned_queue=None.
    ticket_repo = FakeTicketRepo()
    app = create_app(repo=ticket_repo)
    test_client = TestClient(app)
    mcp_client = HttpTicketDbMcpClient(base_url="http://testserver", session=test_client)
    spy_a2a = SpyA2AClient()

    mock_s1 = MagicMock(spec=RouterS1Client)
    # Tier 1 returns low confidence and intent="other", falling into Tier 2 band
    mock_s1.route.return_value = _make_prediction(
        intent="other",
        intent_prob=0.55,
        escalate=False,
        escalate_prob=0.04,
    )

    # Tier 2 returns out_of_scope=True
    mock_t2 = MagicMock()
    mock_t2.return_value = _make_prediction(
        intent="other",
        intent_prob=0.85,
        escalate=False,
        escalate_prob=0.03,
        out_of_scope=True,
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1,
        tier2_invoker=mock_t2,
    )
    router_agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=spy_a2a,
    )
    adk_agent = TriageRouterAdkAgent(router=router_agent)

    # 1. Triage the out-of-scope query
    result = router_agent.triage(
        session_id="sess-oos-100",
        message_text="give me a stock price of google",
        ticket_id="TICK-OOS-001",
    )

    # 2. Verify routing decision
    assert result.decision.decision == "reject_oos"
    assert result.decision.queue == "none"
    assert result.decision.escalate is False
    assert result.decision.predicted_intent == "other"
    assert result.decision.reason == "tier2_out_of_scope"

    # 3. Verify A2A handoff was skipped
    assert len(spy_a2a.calls) == 0

    # 4. Verify answer text from extract_answer_from_result and ADK agent query
    extracted_text = extract_answer_from_result(result)
    assert "TICK-OOS-001" in extracted_text
    assert "Global Retail Hub" in extracted_text
    assert "2 hours" not in extracted_text
    assert "specialist" not in extracted_text

    adk_out = adk_agent.query(
        "give me a stock price of google",
        session_id="sess-oos-100",
    )
    assert adk_out["decision"] == "reject_oos"
    assert adk_out["queue"] == "none"
    assert adk_out["ticket_id"] in adk_out["answer"]
    assert "Global Retail Hub" in adk_out["answer"]
    assert "2 hours" not in adk_out["answer"]

    # 5. Verify persistence in FakeTicketRepo: status == resolved_auto, assigned_queue is None
    all_tickets = list(ticket_repo._tickets.values())
    assert len(all_tickets) == 1
    stored_ticket = all_tickets[0]
    assert stored_ticket.status == "resolved_auto"
    assert stored_ticket.assigned_queue is None

    # 6. Verify HumanQueueAgent never sees the ticket as open
    human_queue = HumanQueueAgent(ticket_repo=ticket_repo)
    assert human_queue.list_open_queue() == []
    depths = human_queue.queue_depths()
    assert all(v == 0 for v in depths.values())
    assert {k: v for k, v in depths.items() if v > 0} == {}


def test_add_only_escalation_supersedes_out_of_scope():
    # REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
    # REQ-AGENT-022: Out-of-Scope Query Refusal via Tier-2 System Prompt
    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_t2 = MagicMock()

    # Even if Tier 2 says out_of_scope=True:
    mock_t2.return_value = _make_prediction(
        intent="other",
        intent_prob=0.90,
        escalate=False,
        escalate_prob=0.05,
        out_of_scope=True,
    )
    mock_s1.route.return_value = _make_prediction(
        intent="other",
        intent_prob=0.50,
        escalate=False,
        escalate_prob=0.10,
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1,
        tier2_invoker=mock_t2,
    )

    # (a) Tier 0 abuse/emergency supersedes out_of_scope
    dec_t0 = policy.evaluate(
        text="give me a stock price of google you idiot",
        session_id="s1",
        customer_id="c1",
    )
    assert dec_t0.decision == "escalate"
    assert dec_t0.escalate is True
    assert dec_t0.queue == "human-standard"

    # (b) Tier 1 high escalate_prob (0.80) supersedes out_of_scope
    mock_s1.route.return_value = _make_prediction(
        intent="other",
        intent_prob=0.50,
        escalate=True,
        escalate_prob=0.80,
    )
    dec_t1 = policy.evaluate(
        text="give me a stock price of google",
        session_id="s2",
        customer_id="c1",
    )
    assert dec_t1.decision == "escalate"
    assert dec_t1.escalate is True

    # (c) Tier 2 angry / immediate / escalate=True supersedes out_of_scope
    mock_s1.route.return_value = _make_prediction(
        intent="other",
        intent_prob=0.50,
        escalate=False,
        escalate_prob=0.10,
    )
    mock_t2.return_value = _make_prediction(
        intent="other",
        intent_prob=0.85,
        escalate=True,
        escalate_prob=0.88,
        sentiment="angry",
        urgency="immediate",
        out_of_scope=True,
    )
    dec_t2 = policy.evaluate(
        text="give me a stock price of google right now",
        session_id="s3",
        customer_id="c1",
    )
    assert dec_t2.decision == "escalate"
    assert dec_t2.escalate is True
    assert dec_t2.queue == "human-standard"


def test_in_domain_other_still_escalates_to_human_standard():
    # REQ-AGENT-022: In-domain support queries with intent="other" (e.g. app crashes) escalate to human-standard.
    # REQ-AGENT-005: Calming ack and queue persistence for human escalation.
    ticket_repo = FakeTicketRepo()
    app = create_app(repo=ticket_repo)
    test_client = TestClient(app)
    mcp_client = HttpTicketDbMcpClient(base_url="http://testserver", session=test_client)
    spy_a2a = SpyA2AClient()

    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = _make_prediction(
        intent="other",
        intent_prob=0.60,
        escalate=False,
        escalate_prob=0.10,
    )

    # In-domain retail issue with no self-service FAQ -> out_of_scope=False, escalate=True
    mock_t2 = MagicMock()
    mock_t2.return_value = _make_prediction(
        intent="other",
        intent_prob=0.85,
        escalate=True,
        escalate_prob=0.85,
        sentiment="frustrated",
        urgency="normal",
        out_of_scope=False,
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1,
        tier2_invoker=mock_t2,
    )
    router_agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=spy_a2a,
    )

    result = router_agent.triage(
        session_id="sess-crash-1",
        message_text="Your mobile app crashes whenever I tap checkout",
        ticket_id="TICK-CRASH-001",
    )

    assert result.decision.decision == "escalate"
    assert result.decision.queue == "human-standard"
    assert result.decision.escalate is True
    assert result.decision.predicted_intent == "other"
    assert result.calming_ack is not None
    assert "2 hours" in result.calming_ack

    # Ticket in repo is open and assigned to human-standard
    all_tickets = list(ticket_repo._tickets.values())
    assert len(all_tickets) == 1
    assert all_tickets[0].status == "open"
    assert all_tickets[0].assigned_queue == "human-standard"

    # Human queue agent sees 1 open ticket in human-standard
    human_queue = HumanQueueAgent(ticket_repo=ticket_repo)
    open_q = human_queue.list_open_queue()
    assert len(open_q) == 1
    assert open_q[0].assigned_queue == "human-standard"
    assert human_queue.queue_depths()["human-standard"] == 1


def test_tier2_system_instruction_wiring_and_legacy_compat(monkeypatch):
    # REQ-AGENT-022: Tier-2 system instruction loading and invocation.
    # REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
    sys_inst_v3 = get_tier2_system_instruction("v3")
    assert sys_inst_v3 is not None
    assert "Global Retail Hub" in sys_inst_v3
    assert get_tier2_system_instruction("v1") is None
    assert get_tier2_system_instruction("v2") is None

    recorded_calls = []

    class FakeGenaiClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(generate_content=self._generate_content)

        def _generate_content(self, model, contents, **kwargs):
            recorded_calls.append({"model": model, "contents": contents, "kwargs": kwargs})
            if "config" in kwargs:
                return SimpleNamespace(
                    text='{"escalate": false, "escalate_prob": 0.02, "out_of_scope": true, "sentiment": "neutral", "urgency": "can wait days", "intent": "other", "intent_prob": 0.95}'
                )
            return SimpleNamespace(
                text='{"escalate": false, "escalate_prob": 0.02, "sentiment": "neutral", "urgency": "can wait days", "intent": "other", "intent_prob": 0.95}'
            )

    fake_google = SimpleNamespace(genai=SimpleNamespace(Client=FakeGenaiClient))
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_google.genai)

    # 1. Under active v4 config, config kwarg with system_instruction is passed
    pred_v4 = invoke_tier2_gemini(
        "give me a stock price of google",
        target_model="test-router-model",
    )
    assert pred_v4.out_of_scope is True
    assert pred_v4.escalate is False
    assert pred_v4.intent == "other"
    assert len(recorded_calls) == 1
    last_call = recorded_calls[-1]
    assert "config" in last_call["kwargs"]
    cfg_arg = last_call["kwargs"]["config"]
    if isinstance(cfg_arg, dict):
        assert "Global Retail Hub" in cfg_arg.get("system_instruction", "")
    else:
        assert hasattr(cfg_arg, "system_instruction")

    # 1b. Under v3 config, config kwarg with system_instruction is also passed
    token_v3 = _CONTEXT_TIER2_VERSION.set("v3")
    try:
        pred_v3 = invoke_tier2_gemini(
            "give me a stock price of google",
            target_model="test-router-model",
        )
        assert pred_v3.out_of_scope is True
        assert pred_v3.escalate is False
        assert pred_v3.intent == "other"
        last_call_v3 = recorded_calls[-1]
        assert "config" in last_call_v3["kwargs"]
    finally:
        _CONTEXT_TIER2_VERSION.reset(token_v3)

    # 2. Under legacy v1, NO config kwarg is passed
    token = _CONTEXT_TIER2_VERSION.set("v1")
    try:
        pred_v1 = invoke_tier2_gemini(
            "give me a stock price of google",
            target_model="test-router-model",
        )
        assert pred_v1.out_of_scope is False
        last_call_v1 = recorded_calls[-1]
        assert "config" not in last_call_v1["kwargs"]
    finally:
        _CONTEXT_TIER2_VERSION.reset(token)

    # 3. Under legacy v2, NO config kwarg is passed
    token = _CONTEXT_TIER2_VERSION.set("v2")
    try:
        pred_v2 = invoke_tier2_gemini(
            "give me a stock price of google",
            target_model="test-router-model",
        )
        assert pred_v2.out_of_scope is False
        last_call_v2 = recorded_calls[-1]
        assert "config" not in last_call_v2["kwargs"]
    finally:
        _CONTEXT_TIER2_VERSION.reset(token)


def test_packaged_fallback_when_prompts_dir_missing(monkeypatch, tmp_path):
    # REQ-AGENT-017: Fallback to built-in v4/v3 prompt/instruction when prompts dir is absent.
    # REQ-AGENT-022: Out-of-Scope Query Refusal via Tier-2 System Prompt
    monkeypatch.setattr(
        "ticket_router.config.agent_tuning._resolve_prompt_file",
        lambda filename: None,
    )

    # 1. v4 system instruction falls back to _V4_DEFAULT_SYSTEM_INSTRUCTION
    sys_inst_v4 = get_tier2_system_instruction("v4")
    assert sys_inst_v4 is not None
    assert "Global Retail Hub" in sys_inst_v4
    assert "stock prices or financial market quotes" in sys_inst_v4
    assert get_tier2_system_instruction() == sys_inst_v4

    # 1b. v3 system instruction falls back to _V3_DEFAULT_SYSTEM_INSTRUCTION
    sys_inst_v3 = get_tier2_system_instruction("v3")
    assert sys_inst_v3 is not None
    assert "Global Retail Hub" in sys_inst_v3
    assert "stock prices or financial market quotes" in sys_inst_v3

    # 2. v4 prompt template falls back to _V4_DEFAULT_TEMPLATE
    prompt_v4 = get_tier2_prompt("give me a stock price of google", version="v4")
    assert "give me a stock price of google" in prompt_v4
    assert "out_of_scope (bool)" in prompt_v4
    assert "order_status/shipping_policy/returns_refunds/account_payment/other" in prompt_v4
    assert get_tier2_prompt("give me a stock price of google") == prompt_v4

    # 2b. v3 prompt template falls back to _V3_DEFAULT_TEMPLATE
    prompt_v3 = get_tier2_prompt("give me a stock price of google", version="v3")
    assert "give me a stock price of google" in prompt_v3
    assert "out_of_scope (bool)" in prompt_v3
    assert "order_status/shipping_policy/returns_refunds/account_payment/other" in prompt_v3

    # 3. v1 has no system instruction (returns None)
    assert get_tier2_system_instruction("v1") is None
    assert get_tier2_system_instruction("v2") is None

    # 4. v1 prompt returns legacy default byte-for-byte matching prompt
    prompt_v1 = get_tier2_prompt("give me a stock price of google", version="v1")
    assert "Evaluate the following support ticket:" in prompt_v1
    assert "sentiment (str: neutral/angry)" in prompt_v1
    assert "out_of_scope" not in prompt_v1

