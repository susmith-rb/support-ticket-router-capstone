"""Tests for conversational greeting, capability opener, and pleasantry handling.

# REQ-AGENT-024: Conversational Greeting and Pleasantry Handling
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    extract_answer_from_result,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import HttpTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
    evaluate_tier0_rules,
)
from ticket_router.mcp.ticket_db.server import create_app
from ticket_router.models.router_s1_client import RouterS1Client
from ticket_router.store.fake import FakeTicketRepo


class SpyA2AClient:
    """Spy implementation of A2A client recording all handoff calls."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def handoff(self, payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        return {"status": "AUTO_RESOLVED", "route": "resolved"}


# REQ-AGENT-024
def test_conversational_greetings_do_not_escalate():
    """Verify standalone greetings, capability openers, and pleasantries auto-resolve without escalation."""
    test_messages = [
        ("hey", "greeting"),
        ("hi", "greeting"),
        ("Hello!", "greeting"),
        ("good morning", "greeting"),
        ("Good afternoon team", "greeting"),
        ("hey how can you help", "greeting"),
        ("what can you help me with?", "greeting"),
        ("thanks", "pleasantry"),
        ("thank you so much!", "pleasantry"),
    ]

    for idx, (msg, expected_cat) in enumerate(test_messages):
        # 1. Tier 0 rules verification
        t0 = evaluate_tier0_rules(msg)
        assert t0.matched is True, f"Expected {msg!r} to match Tier 0 rules"
        assert t0.escalate is False, f"Expected {msg!r} to not escalate"
        assert t0.category == expected_cat, f"Expected category {expected_cat} for {msg!r}, got {t0.category}"

        # 2. End-to-end routing and persistence verification
        ticket_repo = FakeTicketRepo()
        app = create_app(repo=ticket_repo)
        test_client = TestClient(app)
        mcp_client = HttpTicketDbMcpClient(base_url="http://testserver", session=test_client)
        spy_a2a = SpyA2AClient()
        mock_s1 = MagicMock(spec=RouterS1Client)
        mock_t2 = MagicMock()

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

        session_id = f"sess-greet-{idx}"
        ticket_id = f"TICK-GREET-{idx:03d}"

        result = router_agent.triage(
            session_id=session_id,
            message_text=msg,
            ticket_id=ticket_id,
        )

        assert result.decision.decision == "auto_rag"
        assert result.decision.queue == "none"
        assert result.decision.tier == "tier0_rules"
        assert result.decision.predicted_intent == "other"
        assert result.decision.escalate is False
        assert result.calming_ack is None

        # Verify spy counts: downstream models/agents must NOT be called
        assert len(spy_a2a.calls) == 0
        assert mock_s1.route.call_count == 0
        assert mock_t2.call_count == 0

        # User-facing answer checks
        extracted_answer = extract_answer_from_result(result)
        if expected_cat == "greeting":
            assert "Global Retail Hub" in extracted_answer
        else:
            assert "You're welcome" in extracted_answer

        assert "#TICK-" not in extracted_answer
        assert "within 2 hours" not in extracted_answer
        assert "logged ticket" not in extracted_answer

        # Test ADK agent query method
        adk_out = adk_agent.query(msg, session_id=session_id)
        assert adk_out["decision"] == "auto_rag"
        assert adk_out["queue"] == "none"
        assert adk_out["calming_ack"] is None
        assert adk_out["intent"] == "other"
        if expected_cat == "greeting":
            assert "Global Retail Hub" in adk_out["answer"]
        else:
            assert "You're welcome" in adk_out["answer"]
        assert "#TICK-" not in adk_out["answer"]
        assert "within 2 hours" not in adk_out["answer"]
        assert "logged ticket" not in adk_out["answer"]

        # 3. Storage verification: FakeTicketRepo status == resolved_auto, assigned_queue is None
        all_tickets = list(ticket_repo._tickets.values())
        assert len(all_tickets) == 1
        stored_ticket = all_tickets[0]
        assert stored_ticket.status == "resolved_auto"
        assert stored_ticket.assigned_queue is None

        # 4. HumanQueueAgent verification: list_open_queue is empty
        human_queue = HumanQueueAgent(ticket_repo=ticket_repo)
        assert human_queue.list_open_queue() == []


# REQ-AGENT-003, REQ-AGENT-024
def test_greeting_add_only_escalation_and_negative_guards():
    """Verify add-only escalation takes precedence over greetings and negative guards reject inquiries."""
    # REQ-AGENT-003: Safety/abuse/human-request triggers always escalate
    escalation_cases = [
        ("hey you idiot", "abuse"),
        ("hello I am calling my lawyer", "legal"),
        ("hi I want to speak to a human agent", "human_request"),
    ]
    for msg, expected_cat in escalation_cases:
        t0 = evaluate_tier0_rules(msg)
        assert t0.matched is True, f"Expected {msg!r} to match Tier 0"
        assert t0.escalate is True, f"Expected {msg!r} to escalate"
        assert t0.category == expected_cat, f"Expected category {expected_cat} for {msg!r}"

    # Negative guards: vague distress cries, combined inquiries, social handles
    negative_guards = [
        "not working ?",
        "help me",
        "can you help ?",
        "can you help me?",
        "why 2 hours ?",
        "hey where is my order",
        "hi what is your return policy",
        "thanks, where is ORD-10001?",
        "@AmazonHelp hi",
    ]
    for msg in negative_guards:
        t0 = evaluate_tier0_rules(msg)
        assert t0.matched is False, f"Expected {msg!r} to NOT match Tier 0 rules, got {t0}"


# REQ-AGENT-024
def test_greeting_rules_zero_false_matches_on_eval_benchmarks():
    """Verify Tier 0 greeting rules cause zero false auto-rag short-circuits across benchmark datasets."""
    repo_root = Path(__file__).resolve().parents[2]

    # 1. Check routing_v2.1.0 benchmark splits
    routing_splits = ["dev", "heldout", "regression"]
    for split in routing_splits:
        split_path = repo_root / "evals" / "data" / "routing_v2.1.0" / f"{split}.jsonl"
        assert split_path.is_file(), f"Missing split file: {split_path}"
        with open(split_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                text = row.get("description") or row.get("text") or ""
                exp = row.get("expected") or {}
                exp_decision = exp.get("decision")
                exp_escalate = exp.get("escalate")

                t0 = evaluate_tier0_rules(text)
                if exp_decision in ("escalate", "reject_oos") or exp_escalate is True:
                    assert not (t0.matched and not t0.escalate), (
                        f"False greeting match in routing_v2.1.0 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}, t0={t0}"
                    )

    # 2. Check agent_e2e_v1 benchmark splits
    for split in ["dev", "test"]:
        split_path = repo_root / "evals" / "data" / "agent_e2e_v1" / f"{split}.jsonl"
        assert split_path.is_file(), f"Missing split file: {split_path}"
        with open(split_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if not line.strip():
                    continue
                row = json.loads(line)
                text = row.get("message") or ""
                exp_decision = row.get("expected_decision")

                t0 = evaluate_tier0_rules(text)
                if exp_decision in ("escalate", "reject_oos"):
                    assert not (t0.matched and not t0.escalate), (
                        f"False greeting match in agent_e2e_v1 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}, t0={t0}"
                    )

    # 3. Explicitly verify REG-0003 ('hey how can you help') behavior
    reg_0003_text = "hey how can you help"
    policy = TriagePolicy(thresholds=RouterThresholds())
    decision = policy.evaluate(
        text=reg_0003_text,
        session_id="reg-0003-sess",
        customer_id="anon:reg0003",
    )
    assert decision.decision == "auto_rag"
    assert decision.predicted_intent == "other"
    assert decision.escalate is False
    assert decision.queue == "none"
    assert decision.tier == "tier0_rules"
