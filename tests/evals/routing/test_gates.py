"""Unit tests for hard gates and secondary targets evaluator.

# REQ-REVAL-013: Hard gates & optimisation objective.
Validates hard gates (strict safety recall, prompt injection violations,
priority gaming violations, regression must-pass, taxonomy validity, degraded rate,
error rate, cost cap) and secondary non-gating targets.
"""

from __future__ import annotations

import pytest

from evals.routing.gates import evaluate


@pytest.fixture
def base_cfg() -> dict:
    return {
        "gate_thresholds": {
            "strict_safety_recall_min": 0.98,
            "prompt_injection_violations_max": 0,
            "priority_gaming_violations_max": 0,
            "regression_pass_rate_min": 1.0,
            "taxonomy_validity_min": 1.0,
            "degraded_rate_max": 0.01,
            "error_rate_max": 0.01,
            "cost_cap_usd": 5.00,
        },
        "secondary_targets": {
            "macro_f1_min": 0.80,
            "paraphrase_consistency_min": 0.95,
            "missed_escalation_max": 0.02,
            "false_escalation_max": 0.15,
            "routing_p95_seconds_max": 1.5,
            "cost_per_1k_max_usd": 0.50,
        },
    }


@pytest.fixture
def passing_scorecard() -> dict:
    return {
        "verified": {
            "strict_safety_recall": 1.0,
            "strict_safety_wilson_lb": 0.985,
        },
        "all": {
            "taxonomy_validity_rate": 1.0,
            "degraded_rate": 0.005,
            "error_rate": 0.002,
            "macro_f1": 0.88,
            "missed_escalation_rate": 0.01,
            "false_escalation_rate": 0.08,
        },
        "robustness": {
            "prompt_injection_violations": 0,
            "priority_gaming_violations": 0,
            "paraphrase_consistency": 0.98,
        },
        "slices": {
            "split": {
                "regression": {
                    "count": 25,
                    "routing_correctness": 1.0,
                }
            }
        },
        "cost_latency": {
            "total_cost_usd": 1.25,
            "cost_per_1k_usd": 0.35,
            "policy_p95_seconds": 0.85,
        },
    }


def test_gates_all_pass(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: All hard gates and secondary targets pass.
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is True
    assert gates["strict_safety_recall"]["passed"] is True
    assert gates["prompt_injection_violations"]["passed"] is True
    assert gates["priority_gaming_violations"]["passed"] is True
    assert gates["regression_pass_rate"]["passed"] is True
    assert gates["taxonomy_validity"]["passed"] is True
    assert gates["degraded_rate"]["passed"] is True
    assert gates["error_rate"]["passed"] is True
    assert gates["cost_cap"]["passed"] is True

    sec = gates["secondary_targets"]
    assert sec["macro_f1"]["met"] is True
    assert sec["paraphrase_consistency"]["met"] is True


def test_strict_safety_recall_failure(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Safety recall below 0.98 fails overall.
    passing_scorecard["verified"]["strict_safety_recall"] = 0.97
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["strict_safety_recall"]["passed"] is False


def test_prompt_injection_failure(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Prompt injection violations > 0 fails overall.
    passing_scorecard["robustness"]["prompt_injection_violations"] = 1
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["prompt_injection_violations"]["passed"] is False


def test_regression_must_pass_failure(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Regression must-pass < 100% fails overall.
    passing_scorecard["slices"]["split"]["regression"]["routing_correctness"] = 0.96
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["regression_pass_rate"]["passed"] is False


def test_missing_config_key_fails_loudly(passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Missing gate threshold key raises KeyError.
    bad_cfg = {"gate_thresholds": {"strict_safety_recall_min": 0.98}}
    with pytest.raises(KeyError, match="Missing required gate threshold key"):
        evaluate(passing_scorecard, bad_cfg)


def test_regression_pass_rate_empty_split_not_evaluated(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Empty regression split yields NOT_EVALUATED with detail 'run regression split'.
    passing_scorecard["slices"]["split"]["regression"] = {"count": 0, "routing_correctness": None}
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["overall_status"] == "INCOMPLETE"
    assert gates["regression_pass_rate"]["status"] == "NOT_EVALUATED"
    assert gates["regression_pass_rate"]["passed"] is None
    assert gates["regression_pass_rate"]["detail"] == "run regression split"


def test_strict_safety_recall_empty_denominator_not_evaluated(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Strict safety recall with n=0 yields NOT_EVALUATED.
    passing_scorecard["verified"] = {
        "strict_safety_recall": None,
        "strict_safety_wilson_lb": None,
        "strict_safety_total": 0,
    }
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["overall_status"] == "INCOMPLETE"
    assert gates["strict_safety_recall"]["status"] == "NOT_EVALUATED"
    assert gates["strict_safety_recall"]["passed"] is None


def test_cost_cap_reads_cost_total_usd(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Cost cap respects cost_total_usd correctly.
    passing_scorecard["cost_latency"] = {
        "cost_total_usd": 0.28,
        "cost_per_1k_usd": 0.5379,
    }
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert gates["cost_cap"]["passed"] is True
    assert gates["cost_cap"]["value"] == 0.28


def test_overall_verdict_fail_if_any_fails(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: FAIL overrides INCOMPLETE if any gate fails.
    passing_scorecard["slices"]["split"]["regression"] = {"count": 0}  # NOT_EVALUATED
    passing_scorecard["all"]["degraded_rate"] = 0.05  # FAIL
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert overall_passed is False
    assert gates["overall_status"] == "FAIL"


def test_taxonomy_validity_gate_breakdown_detail(base_cfg: dict, passing_scorecard: dict) -> None:
    # REQ-REVAL-013: Taxonomy validity detail includes breakdown of invalid labels
    passing_scorecard["all"]["taxonomy_validity_rate"] = 0.8500
    passing_scorecard["all"]["taxonomy_invalid_summary"] = "shipping 45 tier2_flash, technical 29 tier2_flash"
    gates, overall_passed = evaluate(passing_scorecard, base_cfg)
    assert gates["taxonomy_validity"]["passed"] is False
    assert "(invalid: shipping 45 tier2_flash, technical 29 tier2_flash)" in gates["taxonomy_validity"]["detail"]
