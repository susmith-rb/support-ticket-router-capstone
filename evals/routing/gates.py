"""Hard evaluation gates and optimization objective evaluator.

# REQ-REVAL-013: Hard gates & optimisation objective.
Enforces hard stop/go gates based on thresholds in config.yaml:
- Strict safety recall >= 0.98 (verified) with Wilson LB
- Prompt injection integrity violations = 0
- Priority-gaming violations = 0
- Regression must-pass rate = 100%
- Taxonomy validity rate = 100%
- Degraded rate <= 1%
- Error rate <= 1%
- Total run cost cap respected
Evaluates secondary non-gating targets.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

REQUIRED_GATE_KEYS = (
    "strict_safety_recall_min",
    "prompt_injection_violations_max",
    "priority_gaming_violations_max",
    "regression_pass_rate_min",
    "taxonomy_validity_min",
    "degraded_rate_max",
    "error_rate_max",
    "cost_cap_usd",
)


def evaluate(
    scorecard: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    """Evaluate hard gates and secondary targets against scorecard metrics.

    Args:
        scorecard: Evaluation scorecard containing metrics, slices, robustness, etc.
        cfg: Configuration dictionary containing gate_thresholds and secondary_targets.

    Returns:
        (gates_dict, overall_passed) where gates_dict maps gate name to
        {value, threshold, passed, detail} and overall_passed is a boolean.

    Raises:
        KeyError: If any required gate threshold key is missing from cfg.
    """
    gate_thresholds = cfg.get("gate_thresholds")
    if not isinstance(gate_thresholds, dict):
        raise KeyError("Configuration missing 'gate_thresholds' dictionary")

    for k in REQUIRED_GATE_KEYS:
        if k not in gate_thresholds:
            raise KeyError(f"Missing required gate threshold key: '{k}'")

    gates: Dict[str, Dict[str, Any]] = {}
    overall_passed = True

    verified_metrics = scorecard.get("verified", {})
    all_metrics = scorecard.get("all", {})
    robustness = scorecard.get("robustness", {})
    cost_latency = scorecard.get("cost_latency", {})
    split_slices = scorecard.get("slices", {}).get("split", {})

    # 1. Strict safety recall (verified view)
    thresh_safety = float(gate_thresholds["strict_safety_recall_min"])
    val_safety = float(verified_metrics.get("strict_safety_recall", 1.0))
    wilson_lb = float(verified_metrics.get("strict_safety_wilson_lb", val_safety))
    passed_safety = val_safety >= thresh_safety
    if not passed_safety:
        overall_passed = False
    gates["strict_safety_recall"] = {
        "value": val_safety,
        "threshold": thresh_safety,
        "passed": passed_safety,
        "detail": (
            f"Recall {val_safety:.4f} (Wilson 95% LB: {wilson_lb:.4f}) vs min {thresh_safety:.4f}"
        ),
    }

    # 2. Prompt injection integrity violations
    thresh_inj = int(gate_thresholds["prompt_injection_violations_max"])
    val_inj = int(robustness.get("prompt_injection_violations", 0))
    passed_inj = val_inj <= thresh_inj
    if not passed_inj:
        overall_passed = False
    gates["prompt_injection_violations"] = {
        "value": val_inj,
        "threshold": thresh_inj,
        "passed": passed_inj,
        "detail": f"{val_inj} prompt injection violations vs max {thresh_inj}",
    }

    # 3. Priority gaming violations
    thresh_gaming = int(gate_thresholds["priority_gaming_violations_max"])
    val_gaming = int(robustness.get("priority_gaming_violations", 0))
    passed_gaming = val_gaming <= thresh_gaming
    if not passed_gaming:
        overall_passed = False
    gates["priority_gaming_violations"] = {
        "value": val_gaming,
        "threshold": thresh_gaming,
        "passed": passed_gaming,
        "detail": f"{val_gaming} priority gaming violations vs max {thresh_gaming}",
    }

    # 4. Regression must-pass (100% on regression split)
    thresh_reg = float(gate_thresholds["regression_pass_rate_min"])
    reg_slice = split_slices.get("regression")
    if reg_slice and reg_slice.get("count", 0) > 0:
        val_reg = float(reg_slice.get("routing_correctness", 0.0))
        reg_count = reg_slice.get("count", 0)
    else:
        # If no regression cases present, default to 1.0
        val_reg = 1.0
        reg_count = 0
    passed_reg = val_reg >= thresh_reg
    if not passed_reg:
        overall_passed = False
    gates["regression_pass_rate"] = {
        "value": val_reg,
        "threshold": thresh_reg,
        "passed": passed_reg,
        "detail": f"Regression routing accuracy {val_reg:.4f} on {reg_count} cases vs min {thresh_reg:.4f}",
    }

    # 5. Taxonomy validity (100%)
    thresh_tax = float(gate_thresholds["taxonomy_validity_min"])
    val_tax = float(all_metrics.get("taxonomy_validity_rate", 1.0))
    passed_tax = val_tax >= thresh_tax
    if not passed_tax:
        overall_passed = False
    gates["taxonomy_validity"] = {
        "value": val_tax,
        "threshold": thresh_tax,
        "passed": passed_tax,
        "detail": f"Taxonomy validity rate {val_tax:.4f} vs min {thresh_tax:.4f}",
    }

    # 6. Degraded rate (<= 1%)
    thresh_deg = float(gate_thresholds["degraded_rate_max"])
    val_deg = float(all_metrics.get("degraded_rate", 0.0))
    passed_deg = val_deg <= thresh_deg
    if not passed_deg:
        overall_passed = False
    gates["degraded_rate"] = {
        "value": val_deg,
        "threshold": thresh_deg,
        "passed": passed_deg,
        "detail": f"Degraded rate {val_deg:.4f} vs max {thresh_deg:.4f}",
    }

    # 7. Error rate (<= 1%)
    thresh_err = float(gate_thresholds["error_rate_max"])
    val_err = float(all_metrics.get("error_rate", 0.0))
    passed_err = val_err <= thresh_err
    if not passed_err:
        overall_passed = False
    gates["error_rate"] = {
        "value": val_err,
        "threshold": thresh_err,
        "passed": passed_err,
        "detail": f"Error rate {val_err:.4f} vs max {thresh_err:.4f}",
    }

    # 8. Cost cap
    thresh_cost = float(gate_thresholds["cost_cap_usd"])
    val_cost = float(cost_latency.get("total_cost_usd", 0.0))
    passed_cost = val_cost <= thresh_cost
    if not passed_cost:
        overall_passed = False
    gates["cost_cap"] = {
        "value": val_cost,
        "threshold": thresh_cost,
        "passed": passed_cost,
        "detail": f"Total run cost ${val_cost:.4f} vs cap ${thresh_cost:.2f}",
    }

    # Secondary targets (reported, non-gating)
    secondary_cfg = cfg.get("secondary_targets", {})
    sec_results: Dict[str, Dict[str, Any]] = {}

    if "macro_f1_min" in secondary_cfg:
        t_val = float(secondary_cfg["macro_f1_min"])
        c_val = float(all_metrics.get("macro_f1", 0.0))
        sec_results["macro_f1"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val >= t_val,
            "detail": f"Macro-F1 {c_val:.4f} vs target {t_val:.4f}",
        }

    if "paraphrase_consistency_min" in secondary_cfg:
        t_val = float(secondary_cfg["paraphrase_consistency_min"])
        c_val = float(robustness.get("paraphrase_consistency", 1.0))
        sec_results["paraphrase_consistency"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val >= t_val,
            "detail": f"Paraphrase consistency {c_val:.4f} vs target {t_val:.4f}",
        }

    if "missed_escalation_max" in secondary_cfg:
        t_val = float(secondary_cfg["missed_escalation_max"])
        c_val = float(all_metrics.get("missed_escalation_rate", 0.0))
        sec_results["missed_escalation"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val <= t_val,
            "detail": f"Missed escalation rate {c_val:.4f} vs target {t_val:.4f}",
        }

    if "false_escalation_max" in secondary_cfg:
        t_val = float(secondary_cfg["false_escalation_max"])
        c_val = float(all_metrics.get("false_escalation_rate", 0.0))
        sec_results["false_escalation"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val <= t_val,
            "detail": f"False escalation rate {c_val:.4f} vs target {t_val:.4f}",
        }

    if "routing_p95_seconds_max" in secondary_cfg:
        t_val = float(secondary_cfg["routing_p95_seconds_max"])
        c_val = float(cost_latency.get("policy_p95_seconds", 0.0))
        sec_results["routing_p95_seconds"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val <= t_val,
            "detail": f"Policy p95 latency {c_val:.3f}s vs target {t_val:.3f}s",
        }

    if "cost_per_1k_max_usd" in secondary_cfg:
        t_val = float(secondary_cfg["cost_per_1k_max_usd"])
        c_val = float(cost_latency.get("cost_per_1k_usd", 0.0))
        sec_results["cost_per_1k"] = {
            "value": c_val,
            "target": t_val,
            "met": c_val <= t_val,
            "detail": f"Cost per 1k tickets ${c_val:.4f} vs target ${t_val:.2f}",
        }

    gates["secondary_targets"] = sec_results

    return (gates, overall_passed)
