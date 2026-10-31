"""Unit tests for 4-arm router ablation and 15-bin ECE calibration report.

# REQ-REVAL-012: Deterministic routing metrics (Macro-F1, strict safety recall, ECE).
# REQ-REVAL-018: Cost & latency breakdown across routing tiers.
# REQ-ROUTER-S1-005: Multi-class calibration (15-bin ECE) and metric evaluation.
# REQ-ROUTER-S1-009: Multi-tier routing simulation and add-only escalation invariant.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from evals.routing.ablation import (
    DEFAULT_GOLDEN_CSV,
    DEFAULT_OUT_DIR,
    DEFAULT_RUN027_DIR,
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


def test_hermetic_ablation_builder_and_markdown_rendering(tmp_path: Path) -> None:
    # REQ-ROUTER-S1-009, REQ-REVAL-012, REQ-REVAL-018: hermetic 4-arm cascade & ECE builder
    golden_csv = tmp_path / "golden.csv"
    fieldnames = [
        "case_id",
        "stratum",
        "message",
        "Opus escalate",
        "Sonnet escalate",
        "Opus intent",
        "Sonnet intent",
        "Opus sentiment",
        "Sonnet sentiment",
        "Laya p_esc",
        "Laya escalate",
        "Laya intent",
        "Gemini escalate",
        "Gemini intent",
    ]
    rows = [
        {
            "case_id": "G1",
            "stratum": "strict",
            "message": "I am contacting my lawyer about unauthorized fraud right now!",
            "Opus escalate": "yes",
            "Sonnet escalate": "yes",
            "Opus intent": "account_payment",
            "Sonnet intent": "account_payment",
            "Opus sentiment": "angry",
            "Sonnet sentiment": "angry",
            "Laya p_esc": "0.92",
            "Laya escalate": "yes",
            "Laya intent": "account_payment",
            "Gemini escalate": "yes",
            "Gemini intent": "account_payment",
        },
        {
            "case_id": "G2",
            "stratum": "strict",
            "message": "Brand new $50 gift card says invalid code at checkout, unacceptable!",
            "Opus escalate": "yes",
            "Sonnet escalate": "yes",
            "Opus intent": "account_payment",
            "Sonnet intent": "account_payment",
            "Opus sentiment": "angry",
            "Sonnet sentiment": "angry",
            "Laya p_esc": "0.30",
            "Laya escalate": "no",
            "Laya intent": "account_payment",
            "Gemini escalate": "yes",
            "Gemini intent": "account_payment",
        },
        {
            "case_id": "G3",
            "stratum": "rest",
            "message": "Where is my order ORD-10001?",
            "Opus escalate": "no",
            "Sonnet escalate": "no",
            "Opus intent": "order_status",
            "Sonnet intent": "order_status",
            "Opus sentiment": "neutral",
            "Sonnet sentiment": "neutral",
            "Laya p_esc": "0.05",
            "Laya escalate": "no",
            "Laya intent": "order_status",
            "Gemini escalate": "no",
            "Gemini intent": "order_status",
        },
        {
            "case_id": "G4",
            "stratum": "rest",
            "message": "What is your standard return window for electronics?",
            "Opus escalate": "no",
            "Sonnet escalate": "no",
            "Opus intent": "returns_refunds",
            "Sonnet intent": "returns_refunds",
            "Opus sentiment": "neutral",
            "Sonnet sentiment": "neutral",
            "Laya p_esc": "0.04",
            "Laya escalate": "no",
            "Laya intent": "returns_refunds",
            "Gemini escalate": "no",
            "Gemini intent": "returns_refunds",
        },
    ]
    with open(golden_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    dev_jsonl = tmp_path / "dev.jsonl"
    dev_cases = [
        {"ticket_id": "T1", "expected": {"intent": "returns_refunds", "escalate": False}},
        {"ticket_id": "T2", "expected": {"intent": "account_payment", "escalate": True}},
    ]
    dev_jsonl.write_text("\n".join(json.dumps(c) for c in dev_cases) + "\n", encoding="utf-8")

    run_dir = tmp_path / "run_027"
    run_dir.mkdir()
    traces = [
        {
            "ticket_id": "T1",
            "predicted": {"tier": "tier1_router_s1", "intent": "returns_refunds"},
            "decision_raw": {"details": {"intent_prob": 0.94}},
            "tier_calls": [
                {
                    "tier": "tier1_router_s1",
                    "purpose": "decision",
                    "prediction": {
                        "intent": "returns_refunds",
                        "intent_prob": 0.94,
                        "escalate_prob": 0.05,
                    },
                }
            ],
        },
        {
            "ticket_id": "T2",
            "predicted": {"tier": "tier2_flash", "intent": "account_payment"},
            "decision_raw": {"details": {}},
            "tier_calls": [
                {
                    "tier": "tier1_router_s1",
                    "purpose": "decision",
                    "prediction": {
                        "intent": "account_payment",
                        "intent_prob": 0.72,
                        "escalate_prob": 0.65,
                    },
                }
            ],
        },
    ]
    (run_dir / "traces.jsonl").write_text(
        "\n".join(json.dumps(t) for t in traces) + "\n", encoding="utf-8"
    )
    scorecard = {
        "run_id": "run_027_hermetic",
        "total_cases": 2,
        "metrics": {
            "all": {
                "routing_correctness": 1.0,
                "intent_accuracy": 1.0,
                "macro_f1": 1.0,
                "escalation_recall": 1.0,
                "escalation_precision": 1.0,
                "escalation_f1": 1.0,
                "taxonomy_validity_rate": 1.0,
                "tier_share": {"tier1_router_s1": 0.5, "tier2_flash": 0.5},
                "tier2_invocation_rate": 0.5,
            },
            "verified": {
                "count": 2,
                "routing_correctness": 1.0,
                "intent_accuracy": 1.0,
                "macro_f1": 1.0,
            },
        },
    }
    (run_dir / "scorecard.json").write_text(json.dumps(scorecard), encoding="utf-8")

    eval_test_json = tmp_path / "eval_test.json"
    eval_test_json.write_text(
        json.dumps(
            {
                "data": "heldout_test",
                "n": 646,
                "intent": {"accuracy": 0.8576, "macro_f1": 0.8191, "ece": 0.0526},
                "sentiment": {"accuracy": 0.7802, "macro_f1": 0.7410, "ece": 0.0491},
                "latency_ms": {"p50": 28.4, "p95": 51.2},
            }
        ),
        encoding="utf-8",
    )

    summary = build_ablation_and_ece_summary(
        golden_csv=golden_csv,
        dev_jsonl=dev_jsonl,
        run_dir=run_dir,
        eval_test_json=eval_test_json,
    )
    arms = summary["four_arm_golden_ablation"]["arms"]
    assert arms["hybrid"]["strict_angry_urgent_recall"] == 1.0
    assert arms["hybrid"]["strict_angry_urgent_recall"] > arms["laya_only"]["strict_angry_urgent_recall"]
    assert arms["hybrid"]["tier2_llm_invocation_share"] == 0.25

    md = render_markdown_report(summary)
    assert "Four-Arm Router Architecture Ablation" in md
    assert "15-Bin Expected Calibration Error" in md


def test_committed_four_arm_ablation_and_v21_calibration_invariants() -> None:
    # REQ-ROUTER-S1-009, REQ-REVAL-012, REQ-REVAL-018: committed summary artifact invariants
    committed_path = DEFAULT_OUT_DIR / "ablation_ece_summary.json"
    assert committed_path.is_file(), f"Missing committed summary artifact: {committed_path}"
    summary = json.loads(committed_path.read_text(encoding="utf-8"))

    # When local un-ignored raw CSV/traces are present, also verify exact reproducibility
    if DEFAULT_GOLDEN_CSV.is_file() and (DEFAULT_RUN027_DIR / "traces.jsonl").is_file():
        recomputed = build_ablation_and_ece_summary()
        assert recomputed == summary

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
