"""Unit tests for cost and latency calculations.

# REQ-REVAL-018: Cost & latency breakdown.
"""

from __future__ import annotations

import pytest

from evals.routing.cost import breakdown, call_cost, compute_percentiles, estimate_dry_run_cost
from evals.routing.trace import TraceRecord


@pytest.fixture
def eval_config():
    return {
        "prices": {
            "router_tier2": {
                "input_per_million": 0.15,
                "output_per_million": 0.60,
            },
            "tier2_annotator": {
                "input_per_million": 0.15,
                "output_per_million": 0.60,
            },
        },
        "cost": {
            "tier1": {
                "monthly_endpoint_usd": 50.0,
                "monthly_volume": 100000,
            },
            "tier0": {
                "cost_per_call": 0.0,
            },
            "model_armor": {
                "cost_per_call": 0.0,
            },
        },
    }


def test_call_cost_tier2_gemini_flash(eval_config):
    """REQ-REVAL-018: Tier 2 pricing = tokens x input/output $/1M."""
    tier_call = {
        "tier": "tier2_flash",
        "model_role": "router_tier2",
        "usage": {
            "prompt_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
        },
    }
    # (1000 * 0.15 + 500 * 0.60) / 1M = (150 + 300) / 1M = 450 / 1M = 0.00045
    cost = call_cost(tier_call, eval_config)
    assert pytest.approx(cost, rel=1e-6) == 0.00045


def test_call_cost_tier1_router_s1(eval_config):
    """REQ-REVAL-018: Tier 1 pricing = monthly_endpoint_usd / monthly_volume."""
    tier_call = {
        "tier": "tier1_router_s1",
        "model_role": "router_s1",
    }
    # 50.0 / 100000 = 0.0005
    cost = call_cost(tier_call, eval_config)
    assert pytest.approx(cost, rel=1e-6) == 0.0005


def test_call_cost_tier0_and_model_armor(eval_config):
    """REQ-REVAL-018: Tier 0 deterministic rules and Model Armor cost $0."""
    t0_call = {"tier": "tier0_rules", "model_role": "rules"}
    ma_call = {"tier": "model_armor", "model_role": "model_armor"}
    assert call_cost(t0_call, eval_config) == 0.0
    assert call_cost(ma_call, eval_config) == 0.0


def test_compute_percentiles():
    """REQ-REVAL-018: Percentiles computation helper."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    p = compute_percentiles(vals)
    assert p["p50"] == 30.0
    assert p["max"] == 50.0
    assert p["mean"] == 30.0
    assert p["p95"] > 45.0


def test_breakdown_hand_computed_example(eval_config):
    """REQ-REVAL-018: Complete breakdown numbers match hand-computed values exactly."""
    traces = [
        # Case 1: Tier 0 decided (0 calls to S1/T2)
        TraceRecord(
            ticket_id="TICK-001",
            split="dev",
            target="policy",
            run_id="run_001",
            config_hash="h1",
            candidate="baseline",
            predicted={"tier": "tier0_rules", "decision": "escalate", "intent": "human_escalation", "queue": "human-standard", "escalate": True, "sentiment": "neutral", "urgency": "normal", "degraded": False, "reason": "tier0"},
            decision_raw={"tier": "tier0_rules"},
            tier_calls=[],
            latency_ms_total=1.0,
            cost_usd_total=0.0,
        ),
        # Case 2: Tier 1 decided (1 S1 call)
        TraceRecord(
            ticket_id="TICK-002",
            split="dev",
            target="policy",
            run_id="run_001",
            config_hash="h1",
            candidate="baseline",
            predicted={"tier": "tier1_router_s1", "decision": "auto_rag", "intent": "order_status", "queue": "none", "escalate": False, "sentiment": "neutral", "urgency": "normal", "degraded": False, "reason": "tier1"},
            decision_raw={"tier": "tier1_router_s1"},
            tier_calls=[
                {"tier": "tier1_router_s1", "latency_ms": 20.0, "cost_usd": 0.0005, "model_role": "router_s1"},
            ],
            latency_ms_total=20.0,
            cost_usd_total=0.0005,
        ),
        # Case 3: Tier 2 decided (1 S1 call + 1 T2 call)
        TraceRecord(
            ticket_id="TICK-003",
            split="dev",
            target="policy",
            run_id="run_001",
            config_hash="h1",
            candidate="baseline",
            predicted={"tier": "tier2_flash", "decision": "auto_rag", "intent": "shipping_inquiry", "queue": "none", "escalate": False, "sentiment": "neutral", "urgency": "normal", "degraded": False, "reason": "tier2"},
            decision_raw={"tier": "tier2_flash"},
            tier_calls=[
                {"tier": "tier1_router_s1", "latency_ms": 20.0, "cost_usd": 0.0005, "model_role": "router_s1"},
                {"tier": "tier2_flash", "latency_ms": 100.0, "cost_usd": 0.00045, "model_role": "router_tier2"},
            ],
            latency_ms_total=120.0,
            cost_usd_total=0.00095,
        ),
        # Case 4: Tier 2 decided (1 S1 call + 1 T2 call)
        TraceRecord(
            ticket_id="TICK-004",
            split="dev",
            target="policy",
            run_id="run_001",
            config_hash="h1",
            candidate="baseline",
            predicted={"tier": "tier2_flash", "decision": "escalate", "intent": "billing_issue", "queue": "human-standard", "escalate": True, "sentiment": "neutral", "urgency": "normal", "degraded": False, "reason": "tier2"},
            decision_raw={"tier": "tier2_flash"},
            tier_calls=[
                {"tier": "tier1_router_s1", "latency_ms": 25.0, "cost_usd": 0.0005, "model_role": "router_s1"},
                {"tier": "tier2_flash", "latency_ms": 110.0, "cost_usd": 0.00090, "model_role": "router_tier2"},
            ],
            latency_ms_total=135.0,
            cost_usd_total=0.00140,
        ),
    ]

    bd = breakdown(traces, eval_config)
    assert bd["n_cases"] == 4

    # Total cost = 0.0 + 0.0005 + 0.00095 + 0.00140 = 0.00285
    assert pytest.approx(bd["cost_total_usd"], rel=1e-5) == 0.00285

    # Cost per 1k = 0.00285 / 4 * 1000 = 0.7125
    assert pytest.approx(bd["cost_per_1k_usd"], rel=1e-5) == 0.7125

    # Sliced by deciding tier
    by_dt = bd["by_deciding_tier"]
    assert by_dt["tier0_rules"]["count"] == 1
    assert by_dt["tier0_rules"]["share"] == 0.25
    assert by_dt["tier1_router_s1"]["count"] == 1
    assert by_dt["tier1_router_s1"]["share"] == 0.25
    assert by_dt["tier2_flash"]["count"] == 2
    assert by_dt["tier2_flash"]["share"] == 0.50

    # Per tier calls
    assert bd["per_tier"]["tier1_router_s1"]["calls"] == 3
    assert pytest.approx(bd["per_tier"]["tier1_router_s1"]["cost_usd"], rel=1e-5) == 0.0015
    assert bd["per_tier"]["tier2_flash"]["calls"] == 2
    assert pytest.approx(bd["per_tier"]["tier2_flash"]["cost_usd"], rel=1e-5) == 0.00135

    # Projected monthly cost at 1M and 10M tickets
    cost_per_ticket = 0.00285 / 4  # 0.0007125
    assert pytest.approx(bd["cost_per_ticket_usd"], rel=1e-5) == cost_per_ticket
    assert pytest.approx(bd["projected_monthly_cost"]["at_1M"], rel=1e-4) == 712.50
    assert pytest.approx(bd["projected_monthly_cost"]["at_10M"], rel=1e-4) == 7125.00


def test_estimate_dry_run_cost(eval_config):
    """REQ-REVAL-011 / REQ-REVAL-018: Pre-run cost estimate for dry-run."""
    misses = {
        "tier1_router_s1": 100,
        "tier2_flash": 20,
    }
    # T1: 100 * 0.0005 = 0.05
    # T2 default (350 in, 60 out): (350 * 0.15 + 60 * 0.60) / 1M = (52.5 + 36.0) / 1M = 88.5 / 1M = 0.0000885
    # 20 * 0.0000885 = 0.00177
    # Total = 0.05177
    est = estimate_dry_run_cost(misses, eval_config)
    assert pytest.approx(est, rel=1e-4) == 0.05177
