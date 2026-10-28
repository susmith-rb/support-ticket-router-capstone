"""Tests for dual-target evaluation runner, cost cap, and telemetry schema.

# REQ-EVAL-004: Dual-Target Agent Evaluation Runner
# REQ-EVAL-005: Per-Case Trace Telemetry Schema
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.agent_e2e import (
    DeployedAgentRunner,
    LocalAgentRunner,
    compute_token_cost,
    run_evaluation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runner_dual_targets_and_cost_cap(tmp_path: Path):
    # REQ-EVAL-004
    # 1. Local runner execution
    local_runner = LocalAgentRunner()
    sample_case = {
        "case_id": "TEST-LOCAL-01",
        "message": "Hello, how do I return an unopened item?",
        "customer_id": "cust_anon",
        "expected_decision": "auto_rag",
        "expected_escalate": False,
        "expected_priority": "P2_STANDARD",
        "is_strict_angry_urgent": False,
    }
    local_trace = local_runner.run_case(sample_case)
    assert local_trace["case_id"] == "TEST-LOCAL-01"
    assert "decision" in local_trace
    assert "latency_ms" in local_trace
    assert "cost_usd" in local_trace

    # 2. Deployed runner in dry-run mode
    dep_runner = DeployedAgentRunner(dry_run=True)
    dep_trace = dep_runner.run_case(sample_case)
    assert dep_trace["case_id"] == "TEST-LOCAL-01"
    assert dep_trace.get("dry_run") is True
    assert "decision" in dep_trace

    # 3. Cost cap abort check: pre-run cost estimation exceeding cap raises RuntimeError
    with pytest.raises(RuntimeError, match="Estimated run cost .* exceeds configured cost cap"):
        run_evaluation(
            target="local",
            split="dev",
            limit=20,
            cost_cap=0.0001,  # Excessively low cap should trigger abort
            dry_run=False,
        )


def test_trace_telemetry_schema_and_cost():
    # REQ-EVAL-005
    local_runner = LocalAgentRunner()
    case = {
        "case_id": "TEST-TRACE-01",
        "message": "Where is my package ORD-10001?",
        "customer_id": "cust_1001",
        "demo_user": "1001",
        "expected_decision": "auto_rag",
        "expected_escalate": False,
        "expected_priority": "P2_STANDARD",
        "is_strict_angry_urgent": False,
    }
    trace = local_runner.run_case(case)

    # Mandatory schema keys
    required_keys = [
        "case_id",
        "input_message",
        "decision",
        "tiers_used",
        "escalate",
        "priority",
        "queue",
        "ticket_id",
        "answer",
        "citations",
        "customer_verified",
        "latency_ms",
        "stage_latencies_ms",
        "token_usage",
        "cost_usd",
        "predicted_intent",
    ]
    for key in required_keys:
        assert key in trace, f"Missing telemetry trace key: '{key}'"

    assert isinstance(trace["tiers_used"], list)
    assert isinstance(trace["stage_latencies_ms"], dict)
    assert "total" in trace["stage_latencies_ms"]
    assert trace["cost_usd"] >= 0.0

    # Token cost calculation sanity check
    cost = compute_token_cost(input_tokens=1000, output_tokens=200)
    assert 0.001 < cost < 0.002
