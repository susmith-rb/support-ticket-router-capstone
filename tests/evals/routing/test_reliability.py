"""Unit tests for stochastic-tier reliability evaluator.

# REQ-REVAL-015: Stochastic-tier reliability (k-pass).
Validates k-pass inter-pass agreement, nominal alpha, mean pairwise agreement,
SD of case means vs pass means, unstable case identification, and threshold flagging.
"""

from __future__ import annotations

from evals.routing.reliability import compute


def test_reliability_perfect_agreement() -> None:
    # REQ-REVAL-015: 3 cases, 3 passes with perfect consensus.
    traces = [
        {
            "ticket_id": f"t{i}",
            "reliability": {
                "passes": [
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                ]
            },
        }
        for i in range(1, 4)
    ]

    res = compute(traces)
    assert res["cases_evaluated"] == 3
    assert res["passes_per_case"] == 3
    assert res["all_fields_reliable"] is True
    assert len(res["unstable_tickets"]) == 0

    intent_info = res["fields"]["intent"]
    assert intent_info["krippendorff_alpha"] == 1.0
    assert intent_info["mean_pairwise_agreement"] == 1.0
    assert intent_info["unstable_cases_count"] == 0


def test_reliability_with_disagreement_and_flagging() -> None:
    # REQ-REVAL-015: Cases with disagreement on intent and decision.
    traces = [
        # Case 1: passes disagree completely on intent (all different)
        {
            "ticket_id": "t1",
            "reliability": {
                "passes": [
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                    {"intent": "shipping_policy", "escalate": True, "decision": "escalate"},
                    {"intent": "returns_refunds", "escalate": False, "decision": "auto_rag"},
                ]
            },
        },
        # Case 2: two agree, one disagrees
        {
            "ticket_id": "t2",
            "reliability": {
                "passes": [
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                    {"intent": "order_status", "escalate": False, "decision": "auto_rag"},
                    {"intent": "other", "escalate": True, "decision": "escalate"},
                ]
            },
        },
    ]

    res = compute(traces, cfg={"reliability": {"alpha_min": 0.80}})
    assert res["cases_evaluated"] == 2
    assert "t1" in res["unstable_tickets"]
    assert "t2" in res["unstable_tickets"]

    intent_info = res["fields"]["intent"]
    assert intent_info["unstable_cases_count"] == 2
    assert intent_info["mean_pairwise_agreement"] < 1.0
    # High threshold (0.80) should flag noisy fields
    assert intent_info["is_reliable"] is False
    assert res["all_fields_reliable"] is False


def test_reliability_empty_or_single_pass() -> None:
    # REQ-REVAL-015: Empty traces or traces without k-pass reliability data.
    traces = [
        {"ticket_id": "t1", "reliability": None},
        {"ticket_id": "t2", "reliability": {"passes": [{"intent": "order_status"}]}},  # < 2 passes
    ]
    res = compute(traces)
    assert res["cases_evaluated"] == 0
    assert res["all_fields_reliable"] is True
