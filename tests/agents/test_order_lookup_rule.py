"""Tests for deterministic order-lookup intent rule and pleasantry coverage.

# REQ-AGENT-026: Deterministic Order-Lookup Intent Rule and Pleasantry Coverage
# REQ-AGENT-003: Invariant: Add-Only Escalation and Abuse-Resistant Routing
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
    evaluate_tier0_rules,
    matches_order_lookup,
)
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client

POSITIVE_ORDER_MESSAGES = [
    # Spec REQ-AGENT-026 examples:
    "what are my orders recently ?",
    "what did I buy recently?",
    "show my recent purchases",
    "can you list my orders from the last month",
    "my order history",
    "where is ORD-10042?",
    "status of ORD-10001",
    "track ORD-10151",
    # Regex / prompt examples:
    "what are my orders",
    "what are my recent orders",
    "what are my orders recently",
    "what are my recent orders recently ?",
    "what did i buy",
    "what did i order",
    "what did i order recently",
    "what have I purchased lately",
    "what have i bought recently",
    "show me my orders",
    "list my past orders from the last month",
    "give me my recent orders from the past week",
    "get me my latest purchases from the last 30 days",
    "can you show my recent orders",
    "can you list me my purchases from the last month",
    "can you show me my recent orders from the past week",
    "can you list my orders from the last month",
    "my recent orders",
    "my purchases",
    "my latest orders",
    "check my recent orders",
    "see my orders",
    "order history",
    "where is ORD-10042",
    "where is my order ORD-10042",
    "where's ORD-10042",
    "what's the status of ORD-10101",
    "what is the status of my order ORD-10101",
    "status of my order ORD-10001",
    "track my order ORD-10151",
    "ORD-10001 status",
    "my order ORD-10001 status",
    "status ORD-10001",
]

NON_MATCHING_MESSAGES = [
    "where is my order? this is ridiculous!!!",
    "I want to return ORD-10001",
    "cancel my order ORD-10001",
    "my order ORD-10001 arrived damaged",
    "what is your return policy?",
    "not working ?",
]


# REQ-AGENT-026, REQ-AGENT-003
def test_order_lookup_rule_skips_tier2_safely():
    """Verify calm order-lookup intent rule triggers after Tier 1 and skips Tier 2 safely."""
    # -------------------------------------------------------------------------
    # 1. Positive Acceptance: Uncertain S1 prediction skips Tier 2
    # -------------------------------------------------------------------------
    uncertain_s1 = RouterPrediction(
        escalate=False,
        escalate_prob=0.20,
        intent="other",
        intent_prob=0.40,
        sentiment="neutral",
        sentiment_prob=0.90,
        urgency="normal",
        urgency_prob=0.90,
        latency_ms=15.0,
    )

    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = uncertain_s1
    spy_t2 = MagicMock()

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1,
        tier2_invoker=spy_t2,
    )

    for idx, msg in enumerate(POSITIVE_ORDER_MESSAGES):
        spy_t2.reset_mock()
        mock_s1.route.reset_mock()

        decision = policy.evaluate(
            text=msg,
            session_id=f"sess-pos-{idx}",
            customer_id="cust-1",
        )

        assert decision.decision == "auto_rag", f"Failed for {msg!r}: {decision}"
        assert decision.tier == "tier1_router_s1", f"Failed for {msg!r}: {decision}"
        assert decision.queue == "none", f"Failed for {msg!r}: {decision}"
        assert decision.predicted_intent == "order_status", f"Failed for {msg!r}: {decision}"
        assert decision.escalate is False, f"Failed for {msg!r}: {decision}"
        assert decision.reason == "tier1_order_lookup_rule", f"Failed for {msg!r}: {decision}"
        assert decision.degraded is False, f"Failed for {msg!r}: {decision}"
        assert decision.latency_ms == 15.0, f"Failed for {msg!r}: {decision}"
        assert decision.details.get("category") == "order_lookup", f"Failed for {msg!r}: {decision}"
        assert decision.details.get("escalate_prob") == 0.20, f"Failed for {msg!r}: {decision}"
        assert decision.details.get("intent_prob") == 0.40, f"Failed for {msg!r}: {decision}"

        # Invariant: Tier 2 Flash is NOT called when rule matches
        assert spy_t2.call_count == 0, f"Expected Tier 2 to not be called for {msg!r}"

    # -------------------------------------------------------------------------
    # 2. Negative Case (a): S1 definitive escalation still escalates (REQ-AGENT-003)
    # -------------------------------------------------------------------------
    escalating_s1_variants = [
        # (a.1) High escalate probability (p_esc >= tau_esc)
        RouterPrediction(
            escalate=True,
            escalate_prob=0.90,
            intent="order_status",
            intent_prob=0.95,
            sentiment="neutral",
            sentiment_prob=0.90,
            urgency="normal",
            urgency_prob=0.90,
            latency_ms=15.0,
        ),
        # (a.2) Angry sentiment
        RouterPrediction(
            escalate=False,
            escalate_prob=0.20,
            intent="other",
            intent_prob=0.40,
            sentiment="angry",
            sentiment_prob=0.95,
            urgency="normal",
            urgency_prob=0.90,
            latency_ms=15.0,
        ),
        # (a.3) Immediate urgency
        RouterPrediction(
            escalate=False,
            escalate_prob=0.20,
            intent="other",
            intent_prob=0.40,
            sentiment="neutral",
            sentiment_prob=0.90,
            urgency="immediate",
            urgency_prob=0.95,
            latency_ms=15.0,
        ),
    ]

    for s1_variant in escalating_s1_variants:
        mock_s1.route.return_value = s1_variant
        for idx, msg in enumerate(POSITIVE_ORDER_MESSAGES[:5]):
            spy_t2.reset_mock()
            decision = policy.evaluate(
                text=msg,
                session_id=f"sess-esc-{idx}",
                customer_id="cust-1",
            )
            assert decision.decision == "escalate"
            assert decision.escalate is True
            assert decision.tier == "tier1_router_s1"
            assert decision.reason == "tier1_definitive_escalation"
            assert spy_t2.call_count == 0

    # -------------------------------------------------------------------------
    # 3. Negative Case (b): S1 failure / None means Tier 2 is called
    # -------------------------------------------------------------------------
    t2_return = RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        intent="order_status",
        intent_prob=0.95,
        sentiment="neutral",
        sentiment_prob=0.90,
        urgency="normal",
        urgency_prob=0.90,
        latency_ms=250.0,
    )
    spy_t2.return_value = t2_return

    # S1 client is None
    policy_no_s1 = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=None,
        tier2_invoker=spy_t2,
    )
    spy_t2.reset_mock()
    decision = policy_no_s1.evaluate(
        text="where is ORD-10042",
        session_id="sess-no-s1",
        customer_id="cust-1",
    )
    assert spy_t2.call_count == 1
    assert decision.tier == "tier2_flash"
    assert decision.decision == "auto_rag"

    # S1 client raises exception
    mock_s1.route.side_effect = RuntimeError("S1 endpoint unavailable")
    policy_err_s1 = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=mock_s1,
        tier2_invoker=spy_t2,
    )
    spy_t2.reset_mock()
    decision = policy_err_s1.evaluate(
        text="show my recent purchases",
        session_id="sess-err-s1",
        customer_id="cust-1",
    )
    assert spy_t2.call_count == 1
    assert decision.tier == "tier2_flash"
    assert decision.degraded is True
    mock_s1.route.side_effect = None

    # -------------------------------------------------------------------------
    # 4. Negative Case (c): Non-matching messages call Tier 2 as before
    # -------------------------------------------------------------------------
    mock_s1.route.return_value = uncertain_s1
    for idx, msg in enumerate(NON_MATCHING_MESSAGES):
        spy_t2.reset_mock()
        decision = policy.evaluate(
            text=msg,
            session_id=f"sess-nm-{idx}",
            customer_id="cust-1",
        )
        assert spy_t2.call_count == 1, f"Expected Tier 2 to be called for {msg!r}"
        assert decision.tier == "tier2_flash"

    # -------------------------------------------------------------------------
    # 5. Negative Case (d): Zero false matches across benchmark datasets
    # -------------------------------------------------------------------------
    repo_root = Path(__file__).resolve().parents[2]

    # Check routing_v2.1.0 benchmark splits
    for split in ["dev", "heldout", "regression"]:
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

                if exp_decision in ("escalate", "reject_oos") or exp_escalate is True:
                    assert not matches_order_lookup(text), (
                        f"False order lookup match in routing_v2.1.0 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}"
                    )
                    t0 = evaluate_tier0_rules(text)
                    assert not (t0.matched and not t0.escalate), (
                        f"False pleasantry/greeting match in routing_v2.1.0 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}, t0={t0}"
                    )

    # Check agent_e2e_v1 benchmark splits
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

                if exp_decision in ("escalate", "reject_oos"):
                    assert not matches_order_lookup(text), (
                        f"False order lookup match in agent_e2e_v1 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}"
                    )
                    t0 = evaluate_tier0_rules(text)
                    assert not (t0.matched and not t0.escalate), (
                        f"False pleasantry/greeting match in agent_e2e_v1 {split} line {idx + 1}: "
                        f"text={text!r}, expected_decision={exp_decision}, t0={t0}"
                    )


# REQ-AGENT-026
def test_matches_order_lookup_helper():
    """Verify matches_order_lookup helper function unit semantics directly."""
    for msg in POSITIVE_ORDER_MESSAGES:
        assert matches_order_lookup(msg) is True, f"Expected {msg!r} to match order lookup"

    for msg in NON_MATCHING_MESSAGES:
        assert matches_order_lookup(msg) is False, f"Expected {msg!r} to NOT match order lookup"

    assert matches_order_lookup("") is False
    assert matches_order_lookup("   ") is False
