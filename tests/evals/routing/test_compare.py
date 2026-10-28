"""Unit tests for baseline vs candidate comparison module.

# REQ-REVAL-016: Baseline vs candidate comparison.
Validates paired case matching, discordant pairs McNemar tests, paired bootstrap
confidence intervals, newly failing regression tracking, slice delta flagging,
verdict logic (ACCEPT, ACCEPT_NEUTRAL, REJECT), and hillclimb audit logging.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.routing.compare import compare
from evals.routing.schema import Expected, RoutingCase, Style


def _make_case(ticket_id: str, intent: str, decision: str, queue: str, split: str = "dev") -> RoutingCase:
    return RoutingCase(
        ticket_id=ticket_id,
        content_hash="h1",
        dataset_version="v1.0",
        split=split,
        created_at="2026-10-28T12:00:00Z",
        subject=None,
        description=f"Ticket {ticket_id}",
        channel="web",
        language="en",
        pii_scrubbed=True,
        expected=Expected(
            intent=intent,
            acceptable_intents=(intent,),
            escalate=(decision == "escalate"),
            decision=decision,
            queue=queue,
        ),
        difficulty="medium",
        scenarios=("normal",),
        style=Style(register="casual", length_bucket="medium (20–80)"),
        source="synthetic",
        source_dataset="synthetic_jetski",
        source_ref="ref",
        ground_truth_source="human_adjudicated",
        label_status="verified",
        verified_fields=("intent", "decision", "queue", "escalate"),
        annotations=(),
        adjudication=None,
        group_id=f"grp-{ticket_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
    )


def _make_trace(ticket_id: str, intent: str, decision: str, queue: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "predicted": {
            "intent": intent,
            "decision": decision,
            "queue": queue,
            "escalate": (decision == "escalate"),
            "tier": "tier1_router_s1",
        },
    }


def test_compare_accept_verdict(tmp_path: Path) -> None:
    # REQ-REVAL-016: Candidate improves objective with statistically positive CI.
    # 4 cases: baseline gets 1 correct, candidate gets all 4 correct
    cases = [_make_case(f"t{i}", "order_status", "auto_rag", "none") for i in range(1, 5)]

    b_traces = [
        _make_trace("t1", "order_status", "auto_rag", "none"),
        _make_trace("t2", "order_status", "escalate", "human-standard"),  # incorrect
        _make_trace("t3", "order_status", "escalate", "human-standard"),  # incorrect
        _make_trace("t4", "order_status", "escalate", "human-standard"),  # incorrect
    ]
    c_traces = [_make_trace(f"t{i}", "order_status", "auto_rag", "none") for i in range(1, 5)]

    b_sc = {"objective": {"all": 0.25}, "cost_latency": {"total_cost_usd": 0.10}}
    c_sc = {"objective": {"all": 1.00}, "cost_latency": {"total_cost_usd": 0.10}}

    log_file = tmp_path / "hillclimb_log.jsonl"
    res = compare(b_sc, b_traces, c_sc, c_traces, cases, cfg={"bootstrap_resamples": 100}, log_path=log_file)

    assert res["wins_count"] == 3
    assert res["losses_count"] == 0
    assert res["verdict"] == "ACCEPT"
    assert res["mcnemar"]["routing_correctness_p_value"] < 0.30
    assert "routing_correctness_discordant" in res["mcnemar"]
    assert res["mcnemar"]["routing_correctness_discordant"]["candidate_wins"] == 3
    assert res["mcnemar"]["routing_correctness_discordant"]["baseline_wins"] == 0
    assert "objective" in res["deltas"]
    assert res["deltas"]["objective"]["ci_95"][0] > 0.0

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["attempt_id"] == 1
    assert logged["verdict"] == "ACCEPT"


def test_compare_accept_neutral_verdict(tmp_path: Path) -> None:
    # REQ-REVAL-016: Objective CI spans 0, but cost is strictly better -> ACCEPT_NEUTRAL.
    cases = [_make_case(f"t{i}", "order_status", "auto_rag", "none") for i in range(1, 3)]
    traces = [_make_trace(f"t{i}", "order_status", "auto_rag", "none") for i in range(1, 3)]

    b_sc = {"objective": {"all": 1.0}, "cost_latency": {"total_cost_usd": 0.50}}
    c_sc = {"objective": {"all": 1.0}, "cost_latency": {"total_cost_usd": 0.25}}  # cheaper!

    log_file = tmp_path / "hillclimb_log.jsonl"
    res = compare(b_sc, traces, c_sc, traces, cases, cfg={"bootstrap_resamples": 100}, log_path=log_file)

    assert res["verdict"] == "ACCEPT_NEUTRAL"


def test_compare_newly_failing_regression() -> None:
    # REQ-REVAL-016: Regression case failing in candidate forces REJECT.
    case = _make_case("reg_01", "order_status", "auto_rag", "none", split="regression")
    b_trace = _make_trace("reg_01", "order_status", "auto_rag", "none")
    c_trace = _make_trace("reg_01", "order_status", "escalate", "human-standard")  # regression regression!

    b_sc = {"objective": {"all": 1.0}}
    c_sc = {"objective": {"all": 0.0}}

    res = compare(b_sc, [b_trace], c_sc, [c_trace], [case], cfg={"bootstrap_resamples": 50})
    assert len(res["newly_failing_regression"]) == 1
    assert res["verdict"] == "REJECT"
