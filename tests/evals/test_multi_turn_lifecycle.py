"""Deterministic harness test for multi-turn ticket lifecycle evaluations.

# REQ-AGENT-037: Ticket Lifecycle and Deterministic Issue Continuation
# REQ-LIFE-001: Continuation Decision Policy
# REQ-LIFE-002: Customer Resolution Confirmation Flow
# REQ-LIFE-003: Multi-Turn Lifecycle Regression and Evaluation Harness
# REQ-HIST-004: Multi-Turn Lifecycle and Conversation History Preservation
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import RouterThresholds, TriagePolicy
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.model_armor import FilterVerdict, ModelArmorVerdict


class MockRouterS1ForEval:
    """Mock RouterS1 for multi-turn eval classification."""

    def route(self, text: str) -> RouterPrediction:
        t = text.lower()
        if any(w in t for w in ("manager", "human", "agent", "dispute", "urgent", "smashed", "warranty")):
            return RouterPrediction(
                escalate=True,
                escalate_prob=0.95,
                sentiment="angry",
                sentiment_prob=0.95,
                urgency="high",
                urgency_prob=0.90,
                intent="other",
                intent_prob=0.80,
                probabilities={"intent": {"other": 0.80}},
            )
        if "order" in t or "purchas" in t:
            return RouterPrediction(
                escalate=False,
                escalate_prob=0.05,
                sentiment="neutral",
                sentiment_prob=0.90,
                urgency="normal",
                urgency_prob=0.90,
                intent="order_status",
                intent_prob=0.95,
                probabilities={"intent": {"order_status": 0.95, "other": 0.05}},
            )
        if "return" in t or "refund" in t:
            return RouterPrediction(
                escalate=False,
                escalate_prob=0.08,
                sentiment="neutral",
                sentiment_prob=0.90,
                urgency="normal",
                urgency_prob=0.90,
                intent="returns_refunds",
                intent_prob=0.92,
                probabilities={"intent": {"returns_refunds": 0.92, "other": 0.08}},
            )
        if "shipping" in t or "delivery" in t:
            return RouterPrediction(
                escalate=False,
                escalate_prob=0.05,
                sentiment="neutral",
                sentiment_prob=0.90,
                urgency="normal",
                urgency_prob=0.90,
                intent="shipping_policy",
                intent_prob=0.95,
                probabilities={"intent": {"shipping_policy": 0.95, "other": 0.05}},
            )
        return RouterPrediction(
            escalate=False,
            escalate_prob=0.10,
            sentiment="neutral",
            sentiment_prob=0.85,
            urgency="normal",
            urgency_prob=0.85,
            intent="other",
            intent_prob=0.70,
            probabilities={"intent": {"other": 0.70}},
        )


class MockA2AClientForEval:
    """Mock A2A client handling auto-resolved answers."""

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        t = (payload.redacted_message or "").lower()
        if "order" in t:
            return {
                "status": "AUTO_RESOLVED",
                "answer": "You have 2 past orders: ORD-1001 (Delivered), ORD-1002 (Shipped).",
                "citations": [{"id": "order_history", "text": "ORD-1001, ORD-1002"}],
                "route": "order_lookup",
            }
        if "shipping" in t:
            return {
                "status": "AUTO_RESOLVED",
                "answer": "Standard shipping takes 3-5 business days.",
                "citations": [{"id": "faq_shipping", "text": "3-5 business days"}],
                "route": "faq",
            }
        return {
            "status": "AUTO_RESOLVED",
            "answer": "Our standard return window is 30 days from delivery.",
            "citations": [{"id": "faq_return", "text": "30 days"}],
            "route": "faq",
        }


class MockModelArmorForEval:
    """Mock Model Armor detecting prompt injection."""

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        t = text.lower()
        if "ignore" in t or "confidential" in t or "system prompt" in t or "dump memory" in t:
            return ModelArmorVerdict(
                is_clean=False,
                blocked=True,
                action="BLOCK",
                pi_jb=FilterVerdict(tripped=True, confidence="HIGH"),
                latency_ms=1.0,
            )
        return ModelArmorVerdict(is_clean=True, blocked=False, action="ALLOW", latency_ms=1.0)


def load_eval_cases() -> list[Dict[str, Any]]:
    path = Path("evals/data/multi_turn_lifecycle_v1.jsonl")
    if not path.exists():
        path = Path(__file__).resolve().parent.parent.parent / "evals" / "data" / "multi_turn_lifecycle_v1.jsonl"
    cases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    return cases


@pytest.mark.parametrize("case", load_eval_cases(), ids=lambda c: str(c.get("case_id")))
def test_multi_turn_lifecycle_dataset(case: Dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """Execute each dialogue from multi_turn_lifecycle_v1.jsonl and assert invariants."""
    case_id = case["case_id"]
    mcp_client = InMemoryTicketDbMcpClient()
    s1_client = MockRouterS1ForEval()
    a2a_client = MockA2AClientForEval()
    ma_client = MockModelArmorForEval()
    publisher = FakePublisher()
    human_handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
    )
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=s1_client,  # type: ignore[arg-type]
        model_armor_client=ma_client,  # type: ignore[arg-type]
    )
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=a2a_client,
        human_handoff=human_handoff,
    )

    # Pre-seed tickets for specific cases
    if "HOLD99" in case_id:
        mcp_client.tickets["TICK-HOLD99"] = {
            "ticket_id": "TICK-HOLD99",
            "session_id": "session_prior_99",
            "customer_id": "cust_eval",
            "status": "open",
            "assigned_queue": "human-standard",
            "category": "support",
            "current_owner": "queue:human-standard",
        }
    if "008" in case_id or "SEC888" in case_id:
        mcp_client.tickets["TICK-SEC888"] = {
            "ticket_id": "TICK-SEC888",
            "session_id": case_id,
            "customer_id": "cust_eval",
            "status": "open",
            "assigned_queue": "human-standard",
            "category": "security",
            "current_owner": "queue:human-standard",
            "decision": {
                "decision": "escalate",
                "tier": "model_armor",
                "reason": "model_armor_security_trigger",
                "queue": "human-standard",
                "ticket_id": "TICK-SEC888",
                "session_id": case_id,
            },
        }

    # Set customer identity for cases
    is_anon = "anon" in case_id
    customer_id = None if is_anon else "cust_eval"

    if customer_id:
        from ticket_router.identity.resolver import VerifiedIdentity

        mock_verified = VerifiedIdentity(
            issuer="https://securetoken.google.com/test-project",
            subject="user_sub_eval",
            email_verified=True,
            exp=time.time() + 3600.0,
        )
        monkeypatch.setattr(
            "ticket_router.identity.resolver.verify_customer_token",
            lambda **kwargs: mock_verified,
        )
        mcp_client.register_identity(
            issuer="https://securetoken.google.com/test-project",
            subject="user_sub_eval",
            customer_id=customer_id,
            status="active",
            linked=True,
        )

    for turn_info in case["turns"]:
        turn_num = turn_info["turn"]
        msg = turn_info["message"]

        # Run turn through agent
        res = agent.triage(
            session_id=case_id,
            message_text=msg,
            customer_id_token="tok_eval" if customer_id else None,
        )

        # Invariant checks
        if "expected_decision" in turn_info:
            assert res.decision.decision == turn_info["expected_decision"], (
                f"[{case_id} turn {turn_num}] expected decision {turn_info['expected_decision']}, got {res.decision.decision}"
            )

        if "expected_tier" in turn_info:
            assert res.decision.tier == turn_info["expected_tier"], (
                f"[{case_id} turn {turn_num}] expected tier {turn_info['expected_tier']}, got {res.decision.tier}"
            )

        if "expected_intent" in turn_info:
            assert res.decision.predicted_intent == turn_info["expected_intent"], (
                f"[{case_id} turn {turn_num}] expected intent {turn_info['expected_intent']}, got {res.decision.predicted_intent}"
            )

        cont = turn_info.get("expected_continuation")
        if cont == "CONTINUE":
            assert res.decision.tier == "human_holding", f"[{case_id} turn {turn_num}] expected CONTINUE human_holding"
        elif cont == "NEW_ISSUE":
            assert res.decision.tier != "human_holding", f"[{case_id} turn {turn_num}] expected NEW_ISSUE (not human_holding)"
        elif cont == "STATUS_INQUIRY":
            assert res.decision.tier == "ticket_status", f"[{case_id} turn {turn_num}] expected STATUS_INQUIRY ticket_status"
        elif cont == "RESOLVE":
            assert res.decision.tier == "ticket_lifecycle", f"[{case_id} turn {turn_num}] expected RESOLVE ticket_lifecycle"
        elif cont == "DENIED":
            assert res.decision.tier == "ticket_lifecycle" and res.decision.customer_verified is False, (
                f"[{case_id} turn {turn_num}] expected DENIED ticket_lifecycle"
            )
