"""Acceptance tests for regression dataset and observed failure seeds.

# REQ-REVAL-020: Versioned regression set of observed failures
"""

from __future__ import annotations

import pathlib

from evals.routing.regression import (
    add_regression,
    load_regression_seeds,
)
from evals.routing.schema import validate_case


def test_load_regression_seeds_schema_and_provenance():
    """# REQ-REVAL-020: Regression seeds conform to schema, carrying failure_ref and provisional status."""
    cases = load_regression_seeds()
    assert len(cases) >= 12, f"Expected at least 12 regression seeds, got {len(cases)}"

    for c in cases:
        errs = validate_case(c)
        assert not errs, f"Case {c.ticket_id} invalid: {errs}"

        assert c.split == "regression"
        assert c.source == "synthetic"
        assert c.source_dataset == "observed_failure"
        assert c.ground_truth_source == "rule_derived"
        assert c.label_status == "provisional"
        assert "observed_failure" in c.scenarios
        assert c.failure_ref is not None
        assert c.failure_ref.failure_id.startswith("REG-")


def test_regression_seeds_cover_required_failure_modes():
    """# REQ-REVAL-020: Seeds include degraded fallbacks, Tier 1 misses, gaming, injection, and PII collision."""
    cases = load_regression_seeds()
    descriptions = {c.description: c for c in cases}

    # (a) 2026-10-28 degraded fallbacks
    assert "why 2 hours ?" in descriptions
    assert "not working ?" in descriptions
    assert "hey how can you help" in descriptions
    case_vague = descriptions["not working ?"]
    assert case_vague.expected.intent == "other"
    assert case_vague.expected.escalate is True
    assert case_vague.expected.queue == "human-standard"

    # (b) Tier 1 strict-recall misses from training/router_s1/README.md
    assert "WHERE IS IT" in descriptions
    case_shouting = descriptions["WHERE IS IT"]
    assert case_shouting.expected.intent == "order_status"
    assert case_shouting.expected.escalate is True
    assert case_shouting.expected.sentiment == "angry"

    assert "preciso da minha encomenda urgente por favor" in descriptions
    assert "Oh wonderful, another delay on my order, absolutely fantastic service." in descriptions

    # (c) Priority-gaming and fake-urgency
    fake_urgent_found = any(
        "EMERGENCY" in c.description and c.expected.queue == "none"
        for c in cases
    )
    assert fake_urgent_found, "Anti-gaming fake urgency case missing or incorrectly escalated to urgent queue"

    # (d) Identity / injection probes
    injection_found = any("Ignore previous instructions" in c.description for c in cases)
    assert injection_found, "Prompt injection probe missing from regression seeds"

    # (e) Order-number vs card PII collision (112-1234567-1234567)
    pii_collision_cases = [
        c for c in cases if "112-1234567-1234567" in c.description
    ]
    assert len(pii_collision_cases) >= 1
    for c in pii_collision_cases:
        assert c.expected.intent == "order_status"
        assert c.expected.decision == "auto_rag"


def test_add_regression_monotonic_id(tmp_path: pathlib.Path):
    """# REQ-REVAL-020: add_regression appends newly-observed failures with incrementing REG-NNNN ID."""
    test_yaml = tmp_path / "regression_seeds.yaml"
    initial_content = """
seeds:
  - failure_id: "REG-0001"
    observed_at: "2026-10-28T00:00:00Z"
    symptom: "Test symptom"
    root_cause: "Test cause"
    description: "Sample test"
    expected:
      intent: "order_status"
      acceptable_intents: ["order_status"]
      escalate: false
      decision: "auto_rag"
      queue: "none"
      sentiment: "neutral"
      urgency: 1
    scenarios: ["observed_failure"]
"""
    test_yaml.write_text(initial_content, encoding="utf-8")

    new_case = add_regression(
        text="New observed failure in production",
        symptom="Misrouted order question",
        root_cause="Heuristic failure on edge case",
        expected={
            "intent": "order_status",
            "acceptable_intents": ["order_status"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "sentiment": "neutral",
            "urgency": 1,
        },
        seeds_path=test_yaml,
    )

    assert new_case.failure_ref.failure_id == "REG-0002"
    assert new_case.split == "regression"
    assert new_case.ground_truth_source == "rule_derived"
    assert new_case.label_status == "provisional"

    errs = validate_case(new_case)
    assert not errs, f"Created regression case invalid: {errs}"

    # Verify second addition increments to REG-0003
    case_3 = add_regression(
        text="Third observed failure",
        symptom="Another symptom",
        root_cause="Another cause",
        expected={
            "intent": "shipping_policy",
            "acceptable_intents": ["shipping_policy"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "sentiment": "neutral",
            "urgency": 0,
        },
        seeds_path=test_yaml,
    )
    assert case_3.failure_ref.failure_id == "REG-0003"
