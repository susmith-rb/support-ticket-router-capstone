"""Unit tests for 4-arm router ablation and 15-bin ECE calibration report.

# REQ-REVAL-012: Deterministic routing metrics (Macro-F1, strict safety recall, ECE).
# REQ-REVAL-018: Cost & latency breakdown across routing tiers.
# REQ-ROUTER-S1-005: Multi-class calibration (15-bin ECE) and metric evaluation.
# REQ-ROUTER-S1-009: Multi-tier routing simulation and add-only escalation invariant.
"""

from __future__ import annotations

from evals.routing.ablation import (
    build_ablation_and_ece_summary,
    compute_ece_with_bins,
    render_markdown_report,
)


def test_compute_ece_with_15_bins_perfect_and_miscalibrated() -> None:
    # REQ-ROUTER-S1-005: 15-bin Expected Calibration Error calculation
    perfect = compute_ece_with_bins([1.0, 1.0, 1.0, 1.0], [True, True, True, True], n_bins=15)
    assert perfect["n_bins"] == 15
    assert perfect["ece"] == 0.0
    assert len(perfect["bins"]) == 15
    assert perfect["bins"][-1]["count"] == 4

    overconf = compute_ece_with_bins([0.95, 0.95, 0.95, 0.95], [True, False, True, False], n_bins=15)
    assert abs(overconf["ece"] - 0.45) < 1e-4
    assert abs(overconf["mce"] - 0.45) < 1e-4


def test_four_arm_ablation_and_v21_calibration_invariants() -> None:
    # REQ-ROUTER-S1-009, REQ-REVAL-012, REQ-REVAL-018: 4-arm cascade vs standalone tiers
    summary = build_ablation_and_ece_summary()
    golden = summary["four_arm_golden_ablation"]
    assert golden["n_cases"] == 300
    arms = golden["arms"]
    assert set(arms.keys()) == {"rules_only", "laya_only", "gemini_only", "hybrid"}

    # Hybrid cascade must meet the >= 0.98 strict angry/urgent recall gate and beat Laya-only & Rules-only
    assert arms["hybrid"]["strict_angry_urgent_recall"] >= 0.98
    assert arms["hybrid"]["strict_angry_urgent_recall"] > arms["laya_only"]["strict_angry_urgent_recall"]
    assert arms["hybrid"]["strict_angry_urgent_recall"] > arms["rules_only"]["strict_angry_urgent_recall"]

    # Add-only escalation guarantees hybrid escalation recall >= standalone Laya and standalone Gemini
    assert arms["hybrid"]["escalation_recall"] >= arms["laya_only"]["escalation_recall"]
    assert arms["hybrid"]["escalation_recall"] >= arms["gemini_only"]["escalation_recall"]

    # Hybrid deflects > 60% of Tier-2 LLM calls relative to Gemini-only
    assert arms["hybrid"]["tier2_llm_invocation_share"] < 0.40
    assert arms["gemini_only"]["tier2_llm_invocation_share"] == 1.0
    assert arms["laya_only"]["tier2_llm_invocation_share"] == 0.0

    # 15-bin ECE on routing_v2.1.0/dev and router-s1 v2 held-out test both satisfy <= 0.08
    cal = summary["calibration_and_v21_cascade"]
    intent_ece = cal["routing_v2_1_dev"]["tier1_intent_ece_15bin_deciding_slice"]
    assert intent_ece["n_bins"] == 15
    assert 0.0 < intent_ece["ece"] <= 0.08
    assert cal["router_s1_v2_heldout_test"]["intent_ece_15bin"] <= 0.08

    md = render_markdown_report(summary)
    assert "Four-Arm Router Architecture Ablation" in md
    assert "15-Bin Expected Calibration Error" in md
