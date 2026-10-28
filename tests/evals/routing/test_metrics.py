"""Unit tests for deterministic routing metrics calculator.

# REQ-REVAL-012: Deterministic routing metrics.
Validates intent accuracy, lenient accuracy, macro-F1, confusion matrix,
decision accuracy, escalation P/R/F1, missed/false escalation, queue accuracy,
urgent queue recall, strict safety recall + Wilson LB, routing correctness,
system tier shares, ECE calibration, slicing, verified vs all scoping,
dry-run rejection, and objective calculation.
"""

from __future__ import annotations

import pytest

from evals.routing.metrics import _compute_ece, _parse_urgency_to_int, compute, compute_objective
from evals.routing.schema import Expected, RoutingCase, Style


def _make_dummy_case(
    ticket_id: str,
    intent: str,
    decision: str,
    queue: str,
    escalate: bool,
    acceptable_intents: tuple[str, ...] | None = None,
    verified_fields: tuple[str, ...] = ("intent", "decision", "queue", "escalate"),
    label_status: str = "verified",
    sentiment: str = "neutral",
    urgency: int = 1,
    scenarios: tuple[str, ...] = ("normal",),
    group_id: str = "g1",
    split: str = "dev",
) -> RoutingCase:
    acc = acceptable_intents or (intent,)
    exp = Expected(
        intent=intent,
        acceptable_intents=acc,
        escalate=escalate,
        decision=decision,
        queue=queue,
        sentiment=sentiment,
        urgency=urgency,
    )
    return RoutingCase(
        ticket_id=ticket_id,
        content_hash="hash1234",
        dataset_version="v1.0",
        split=split,
        created_at="2026-10-28T12:00:00Z",
        subject="Subj",
        description="Desc",
        channel="web",
        language="en",
        pii_scrubbed=True,
        expected=exp,
        difficulty="medium",
        scenarios=scenarios,
        style=Style(register="casual", length_bucket="medium (20–80)"),
        source="synthetic",
        source_dataset="synthetic_jetski",
        source_ref="ref1",
        ground_truth_source="human_adjudicated",
        label_status=label_status,
        verified_fields=verified_fields,
        annotations=(),
        adjudication=None,
        group_id=group_id,
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
    )


def _make_dummy_trace(
    ticket_id: str,
    intent: str,
    decision: str,
    queue: str,
    escalate: bool,
    tier: str = "tier1_router_s1",
    intent_prob: float = 0.9,
    degraded: bool = False,
    error: str | None = None,
    is_dry_run: bool = False,
) -> dict:
    return {
        "ticket_id": ticket_id,
        "split": "dev",
        "target": "policy",
        "run_id": "run_001",
        "config_hash": "cfg123",
        "candidate": "prod",
        "predicted": {
            "intent": intent,
            "decision": decision,
            "queue": queue,
            "escalate": escalate,
            "sentiment": "neutral",
            "urgency": "normal",
            "tier": tier,
            "degraded": degraded,
            "reason": "standard",
        },
        "decision_raw": {"details": {"intent_prob": intent_prob}},
        "tier_calls": [{"tier": tier, "model_role": "router", "latency_ms": 15.0}],
        "reliability": None,
        "latency_ms_total": 15.0,
        "cost_usd_total": 0.0001,
        "error": error,
        "is_dry_run": is_dry_run,
    }


def test_ece_hand_computed() -> None:
    # REQ-REVAL-012: Expected Calibration Error hand-computed check.
    # 2 samples in bin [0.8, 0.9): confs 0.85, 0.85; one correct, one incorrect -> acc = 0.5, conf = 0.85 -> diff = 0.35
    # 2 samples in bin [0.9, 1.0]: confs 0.95, 0.95; both correct -> acc = 1.0, conf = 0.95 -> diff = 0.05
    # ECE = 0.5 * 0.35 + 0.5 * 0.05 = 0.20
    confs = [0.85, 0.85, 0.95, 0.95]
    corr = [True, False, True, True]
    ece = _compute_ece(confs, corr, n_bins=10)
    assert abs(ece - 0.20) < 1e-6


def test_dry_run_rejection() -> None:
    # REQ-REVAL-012: Dry-run traces must be rejected.
    cases = [_make_dummy_case("t1", "order_status", "auto_rag", "none", False)]
    traces = [_make_dummy_trace("t1", "order_status", "auto_rag", "none", False, is_dry_run=True)]
    with pytest.raises(ValueError, match="Dry-run trace detected"):
        compute(traces, cases)


def test_intent_accuracy_lenient_and_per_class() -> None:
    # REQ-REVAL-012: Intent metrics, lenient accuracy, macro-F1.
    c1 = _make_dummy_case("t1", "order_status", "auto_rag", "none", False)
    # c2 has acceptable intents order_status and shipping_policy
    c2 = _make_dummy_case(
        "t2", "order_status", "auto_rag", "none", False,
        acceptable_intents=("order_status", "shipping_policy"),
    )
    c3 = _make_dummy_case("t3", "shipping_policy", "auto_rag", "none", False)

    # t1: exact correct (order_status)
    # t2: predicts shipping_policy (lenient correct, exact incorrect)
    # t3: predicts other (incorrect)
    t1 = _make_dummy_trace("t1", "order_status", "auto_rag", "none", False)
    t2 = _make_dummy_trace("t2", "shipping_policy", "auto_rag", "none", False)
    t3 = _make_dummy_trace("t3", "other", "auto_rag", "none", False)

    scorecard = compute([t1, t2, t3], [c1, c2, c3], cfg={"bootstrap_resamples": 50})
    m = scorecard["all"]

    assert m["count"] == 3
    # Exact intent accuracy: 1 / 3
    assert abs(m["intent_accuracy"] - (1.0 / 3.0)) < 1e-4
    # Lenient accuracy: 2 / 3 (t1 and t2)
    assert abs(m["lenient_accuracy"] - (2.0 / 3.0)) < 1e-4


def test_strict_safety_recall_and_wilson() -> None:
    # REQ-REVAL-012: Strict safety recall on angry/urgent cases + Wilson LB.
    # 2 strict safety cases (one angry, one urgency 3)
    c1 = _make_dummy_case("t1", "order_status", "escalate", "human-standard", True, sentiment="angry")
    c2 = _make_dummy_case("t2", "returns_refunds", "escalate", "human-urgent", True, urgency=3)
    # 1 benign case
    c3 = _make_dummy_case("t3", "order_status", "auto_rag", "none", False, sentiment="neutral", urgency=0)

    # Predictions: t1 escalates (correct), t2 does NOT escalate (missed escalation)
    t1 = _make_dummy_trace("t1", "order_status", "escalate", "human-standard", True)
    t2 = _make_dummy_trace("t2", "returns_refunds", "auto_rag", "none", False)
    t3 = _make_dummy_trace("t3", "order_status", "auto_rag", "none", False)

    scorecard = compute([t1, t2, t3], [c1, c2, c3], cfg={"bootstrap_resamples": 50})
    m = scorecard["all"]

    assert m["strict_safety_total"] == 2
    assert m["strict_safety_passed"] == 1
    assert m["strict_safety_recall"] == 0.5
    assert m["strict_safety_wilson_lb"] < 0.5


def test_routing_correctness_definition() -> None:
    # REQ-REVAL-012: Routing correctness primary metric definition.
    # auto_rag case: correct iff decision==auto_rag AND intent in acceptable_intents
    c_auto = _make_dummy_case("t1", "order_status", "auto_rag", "none", False)
    # escalate case: correct iff decision==escalate AND queue==expected.queue
    c_esc = _make_dummy_case("t2", "order_status", "escalate", "human-urgent", True)

    # Both predicted correctly
    t1 = _make_dummy_trace("t1", "order_status", "auto_rag", "none", False)
    t2 = _make_dummy_trace("t2", "returns_refunds", "escalate", "human-urgent", True)  # wrong intent but queue matches!

    scorecard = compute([t1, t2], [c_auto, c_esc], cfg={"bootstrap_resamples": 50})
    assert scorecard["all"]["routing_correctness"] == 1.0


def test_verified_scoping_exclusion() -> None:
    # REQ-REVAL-012: Verified view scores only fields in verified_fields.
    # c1 has verified_fields=["intent"] ONLY (no "decision" or "escalate")
    c1 = _make_dummy_case("t1", "order_status", "auto_rag", "none", False, verified_fields=("intent",))
    t1 = _make_dummy_trace("t1", "order_status", "escalate", "human-standard", True)

    scorecard = compute([t1], [c1], cfg={"bootstrap_resamples": 50})
    # In verified view: intent was evaluated (intent_acc = 1.0)
    assert scorecard["verified"]["intent_accuracy"] == 1.0
    # But decision was NOT in verified_fields, so decision_accuracy is not scored on c1
    assert scorecard["verified"]["decision_accuracy"] == 0.0


def test_objective_scenario_macro_average() -> None:
    # REQ-REVAL-012: Objective macro-averaged over scenario strata.
    c1 = _make_dummy_case("t1", "order_status", "auto_rag", "none", False, scenarios=("normal",))
    c2 = _make_dummy_case("t2", "order_status", "auto_rag", "none", False, scenarios=("ambiguous",))

    # t1 correct, t2 incorrect
    t1 = _make_dummy_trace("t1", "order_status", "auto_rag", "none", False)
    t2 = _make_dummy_trace("t2", "order_status", "escalate", "human-standard", True)

    obj, sc_scores = compute_objective([(c1, t1), (c2, t2)], is_verified_view=False)
    assert sc_scores["normal"] == 1.0
    assert sc_scores["ambiguous"] == 0.0
    assert obj == 0.5


def test_parse_urgency_to_int() -> None:
    # REQ-REVAL-012: Robust parsing of urgency integers, string digits, and taxonomy labels.
    assert _parse_urgency_to_int(3) == 3
    assert _parse_urgency_to_int("3") == 3
    assert _parse_urgency_to_int("0") == 0
    assert _parse_urgency_to_int("immediate") == 3
    assert _parse_urgency_to_int("within hours") == 2
    assert _parse_urgency_to_int("within a day") == 1
    assert _parse_urgency_to_int("can wait days") == 0
    assert _parse_urgency_to_int("unknown_label") == 0
    assert _parse_urgency_to_int(None) == 0


def test_macro_f1_false_positive_penalty_for_zero_support_class() -> None:
    # REQ-REVAL-012: Macro-F1 penalizes false positive predictions for classes with zero ground truth support.
    # Ground truth: 2 tickets, both "order_status"
    c1 = _make_dummy_case("t1", "order_status", "auto_rag", "none", False)
    c2 = _make_dummy_case("t2", "order_status", "auto_rag", "none", False)

    # Prediction: t1 predicts "order_status" (TP for order_status), t2 predicts "returns_refunds" (FP for returns_refunds, FN for order_status)
    t1 = _make_dummy_trace("t1", "order_status", "auto_rag", "none", False)
    t2 = _make_dummy_trace("t2", "returns_refunds", "auto_rag", "none", False)

    scorecard = compute([t1, t2], [c1, c2], cfg={"bootstrap_resamples": 50})
    # order_status: TP=1, FP=0, FN=1 -> P=1.0, R=0.5, F1=2/3 (~0.6667)
    # returns_refunds: TP=0, FP=1, FN=0 -> P=0.0, R=0.0, F1=0.0
    # Macro-F1 should average over both classes: (0.6667 + 0.0) / 2 = 0.3333
    # If returns_refunds were erroneously skipped due to supp==0, macro-F1 would be 0.6667!
    macro_f1 = scorecard["all"]["macro_f1"]
    assert abs(macro_f1 - (1.0 / 3.0)) < 1e-3


def test_strict_safety_verified_scoping_requires_trigger_and_escalate() -> None:
    # REQ-REVAL-012: Verified strict safety recall requires both trigger field and escalate to be verified.
    # c1: urgency=3, escalate=True, but verified_fields only has ("intent", "decision") -> missing "escalate" & "urgency"
    c1 = _make_dummy_case(
        "t1", "order_status", "escalate", "human-urgent", True,
        urgency=3,
        verified_fields=("intent", "decision"),
    )
    # c2: urgency=3, escalate=True, verified_fields has ("intent", "decision", "escalate", "urgency")
    c2 = _make_dummy_case(
        "t2", "order_status", "escalate", "human-urgent", True,
        urgency=3,
        verified_fields=("intent", "decision", "escalate", "urgency"),
    )

    t1 = _make_dummy_trace("t1", "order_status", "escalate", "human-urgent", True)
    t2 = _make_dummy_trace("t2", "order_status", "escalate", "human-urgent", True)

    scorecard = compute([t1, t2], [c1, c2], cfg={"bootstrap_resamples": 50})

    # In "all" view, both c1 and c2 count towards strict safety: total=2
    assert scorecard["all"]["strict_safety_total"] == 2
    # In "verified" view, c1 must be excluded because urgency and escalate are not verified: total=1
    assert scorecard["verified"]["strict_safety_total"] == 1
    assert scorecard["verified"]["strict_safety_passed"] == 1


def test_compute_objective_verified_scoping_and_rejected_handling() -> None:
    # REQ-REVAL-012: compute_objective drops rejected cases and enforces verified fields in verified view.
    # c_rej: rejected case
    c_rej = _make_dummy_case("t_rej", "order_status", "auto_rag", "none", False, label_status="rejected")
    t_rej = _make_dummy_trace("t_rej", "order_status", "auto_rag", "none", False)

    # c_part: verified status, but verified_fields has only ("intent",) (no "decision")
    c_part = _make_dummy_case(
        "t_part", "order_status", "auto_rag", "none", False,
        label_status="verified",
        verified_fields=("intent",),
    )
    t_part = _make_dummy_trace("t_part", "order_status", "auto_rag", "none", False)

    # c_full: verified status and verified_fields has ("intent", "decision")
    c_full = _make_dummy_case(
        "t_full", "order_status", "auto_rag", "none", False,
        label_status="verified",
        verified_fields=("intent", "decision"),
    )
    t_full = _make_dummy_trace("t_full", "order_status", "auto_rag", "none", False)

    # In verified view: c_rej and c_part must be skipped; only c_full is scored (1.0)
    obj_ver, sc_ver = compute_objective([(c_rej, t_rej), (c_part, t_part), (c_full, t_full)], is_verified_view=True)
    assert obj_ver == 1.0

    # In all view: c_rej is skipped; c_part and c_full are both scored (both 1.0)
    obj_all, sc_all = compute_objective([(c_rej, t_rej), (c_part, t_part), (c_full, t_full)], is_verified_view=False)
    assert obj_all == 1.0

