# REQ-RAG-RETR-015: Multi-Relevant Retrieval Measurement & Adjudication Evaluation
"""Unit tests for rag/multi_label_eval.py (REQ-RAG-RETR-015, PLAN4).

Covers:
1. Hand-computed Cohen's kappa on toy tables (binary and multi-class)
2. Hand-computed graded nDCG@5 example against analytical baseline
3. Scope filtering and waterfall exclusion accounting (MAIN vs ALL-support)
4. Monotonicity test: lower bound <= consensus <= upper bound
5. Intent family prefix mapping
6. System run JSON loading (various formats)
7. End-to-end multi-label evaluation run and report generation
"""

import json
import math
import pytest
from multi_label_eval import (
    compute_agreement_stats,
    compute_cohen_kappa,
    compute_graded_dcg,
    compute_graded_idcg,
    compute_graded_ndcg,
    evaluate_multilabel,
    filter_scope,
    generate_evaluation_report,
    infer_intent_from_doc_id,
    load_system_rows,
)


def test_infer_intent_from_doc_id():
    """Verify document ID prefix mapping to intent family per PLAN4."""
    assert infer_intent_from_doc_id("faq-order-status-track-package") == "order_status"
    assert infer_intent_from_doc_id("faq-account-payment-refund-card") == "account_payment"
    assert infer_intent_from_doc_id("faq-returns-refunds-return-window") == "returns_refunds"
    assert infer_intent_from_doc_id("faq-shipping-policy-free-delivery") == "shipping_policy"
    assert infer_intent_from_doc_id("faq-other-general-feedback") == "other"
    assert infer_intent_from_doc_id(None) == "other"
    assert infer_intent_from_doc_id("") == "other"
    assert infer_intent_from_doc_id("unknown-prefix-doc") == "other"


def test_cohen_kappa_hand_computed_binary():
    """Hand-computed Cohen's kappa on a 10-item toy table.

    Rater 1: [T, T, T, T, F, F, F, F, F, F] (4 True, 6 False)
    Rater 2: [T, T, T, F, T, F, F, F, F, F] (4 True, 6 False)
    Joint counts:
    Both True: 3
    Both False: 5
    R1 True, R2 False: 1
    R1 False, R2 True: 1
    Observed p_o = (3 + 5) / 10 = 0.80
    Expected p_e = (0.4 * 0.4) + (0.6 * 0.6) = 0.16 + 0.36 = 0.52
    kappa = (0.80 - 0.52) / (1 - 0.52) = 0.28 / 0.48 = 7 / 12 ≈ 0.5833333333333334
    """
    r1 = [True, True, True, True, False, False, False, False, False, False]
    r2 = [True, True, True, False, True, False, False, False, False, False]
    kappa = compute_cohen_kappa(r1, r2)
    expected_kappa = 7.0 / 12.0
    assert kappa == pytest.approx(expected_kappa, abs=1e-6)


def test_cohen_kappa_perfect_and_zero_agreement():
    """Verify kappa boundary conditions: perfect agreement is 1.0, chance is 0.0."""
    # Perfect agreement
    r1 = ["doc-A", "doc-B", "doc-C", None]
    r2 = ["doc-A", "doc-B", "doc-C", None]
    assert compute_cohen_kappa(r1, r2) == 1.0

    # Constant category (p_e == 1.0, p_o == 1.0)
    assert compute_cohen_kappa([True, True], [True, True]) == 1.0

    # Uniform chance agreement: 4 items, 2 categories
    r3 = ["cat1", "cat1", "cat2", "cat2"]
    r4 = ["cat1", "cat2", "cat1", "cat2"]
    # p_o = 2/4 = 0.5; p_e = 0.5*0.5 + 0.5*0.5 = 0.5; kappa = 0.0
    assert compute_cohen_kappa(r3, r4) == pytest.approx(0.0, abs=1e-6)


def test_compute_agreement_stats():
    """Test agreement statistics computation over toy label dicts."""
    labels_a = {
        "q1": {"support_request": True, "primary": "doc-1", "secondary": ["doc-2"], "relevant_set": ["doc-1", "doc-2"]},
        "q2": {"support_request": True, "primary": "doc-3", "secondary": [], "relevant_set": ["doc-3"]},
        "q3": {"support_request": False, "primary": None, "secondary": [], "relevant_set": []},
    }
    labels_b = {
        "q1": {"support_request": True, "primary": "doc-1", "secondary": ["doc-2", "doc-4"], "relevant_set": ["doc-1", "doc-2", "doc-4"]},
        "q2": {"support_request": True, "primary": "doc-5", "secondary": [], "relevant_set": ["doc-5"]},  # primary disagreement
        "q3": {"support_request": False, "primary": None, "secondary": [], "relevant_set": []},
    }
    stats = compute_agreement_stats(labels_a, labels_b)
    assert stats["n_common_queries"] == 3
    # support_request: identical for all 3 -> kappa 1.0
    assert stats["cohen_kappa_support_request"] == 1.0
    # primary: q1 matches (doc-1), q3 matches (None), q2 differs (doc-3 vs doc-5) -> 2/3 ≈ 0.6667
    assert stats["exact_match_primary"] == pytest.approx(2.0 / 3.0, abs=1e-4)

    # Jaccard:
    # q1: {1, 2} vs {1, 2, 4} -> intersection 2, union 3 -> 2/3
    # q2: {3} vs {5} -> intersection 0, union 2 -> 0.0
    # q3: both empty -> 1.0
    # mean = (2/3 + 0 + 1) / 3 = (5/3) / 3 = 5/9 ≈ 0.5556
    assert stats["mean_jaccard_relevant"] == pytest.approx(5.0 / 9.0, abs=1e-4)


def test_graded_ndcg_hand_computed_example():
    """Hand-computed graded nDCG@5 example.

    Query: primary = "doc-A", secondary = ["doc-B", "doc-C"]
    Gains: primary -> 2.0, secondary -> 1.0, other -> 0.0

    Retrieved ranking:
    1. doc-B -> secondary (gain = 1) -> 1.0 / log2(1 + 1) = 1.0 / 1.0 = 1.0
    2. doc-A -> primary (gain = 2) -> 2.0 / log2(2 + 1) = 2.0 / 1.58496250072 ≈ 1.261859507
    3. doc-D -> other (gain = 0) -> 0.0
    4. doc-E -> other (gain = 0) -> 0.0
    5. doc-F -> other (gain = 0) -> 0.0
    DCG@5 = 1.0 + 1.261859507 = 2.261859507

    Ideal ranking:
    1. doc-A (primary, gain = 2) -> 2.0 / log2(2) = 2.0
    2. doc-B (secondary, gain = 1) -> 1.0 / log2(3) ≈ 0.630929753
    3. doc-C (secondary, gain = 1) -> 1.0 / log2(4) = 0.5
    IDCG@5 = 2.0 + 0.630929753 + 0.5 = 3.130929753

    nDCG@5 = DCG@5 / IDCG@5 = 2.261859507 / 3.130929753 ≈ 0.722424209
    """
    primary = "doc-A"
    secondary = ["doc-B", "doc-C"]
    retrieved = ["doc-B", "doc-A", "doc-D", "doc-E", "doc-F"]

    dcg = compute_graded_dcg(retrieved, primary, secondary, k=5)
    expected_dcg = 1.0 / math.log2(2) + 2.0 / math.log2(3)
    assert dcg == pytest.approx(expected_dcg, abs=1e-6)

    idcg = compute_graded_idcg(primary, secondary, k=5)
    expected_idcg = 2.0 / math.log2(2) + 1.0 / math.log2(3) + 1.0 / math.log2(4)
    assert idcg == pytest.approx(expected_idcg, abs=1e-6)

    ndcg = compute_graded_ndcg(retrieved, primary, secondary, k=5)
    expected_ndcg = expected_dcg / expected_idcg
    assert ndcg == pytest.approx(expected_ndcg, abs=1e-6)
    assert ndcg == pytest.approx(0.722424, abs=1e-4)


def test_scope_filtering_and_exclusion_counts():
    """Verify waterfall exclusions and scope filtering (MAIN vs ALL-support)."""
    labels = {
        "q1": {"support_request": True, "language": "en", "primary": "doc-1"},  # in MAIN
        "q2": {"support_request": False, "language": "en", "primary": "doc-2"}, # not_support_request
        "q3": {"support_request": True, "language": "es", "primary": "doc-3"},  # non_english
        "q4": {"support_request": True, "language": "en", "primary": None},     # no_primary
        "q5": {"support_request": False, "language": "fr", "primary": None},    # not_support_request (waterfall first check)
    }

    in_scope_main, ex_main = filter_scope(labels, scope="MAIN")
    assert len(in_scope_main) == 1
    assert "q1" in in_scope_main
    assert ex_main["not_support_request"] == 2  # q2, q5
    assert ex_main["non_english"] == 1          # q3
    assert ex_main["no_primary"] == 1           # q4
    assert ex_main["total_excluded"] == 4
    assert ex_main["total_in_scope"] == 1
    assert ex_main["total_evaluated"] == 5

    # Check ALL-support scope (non_english is NOT excluded if support_request is True and primary is present)
    in_scope_all, ex_all = filter_scope(labels, scope="ALL-support")
    assert len(in_scope_all) == 2
    assert "q1" in in_scope_all
    assert "q3" in in_scope_all
    assert ex_all["not_support_request"] == 2
    assert ex_all["non_english"] == 0
    assert ex_all["no_primary"] == 1
    assert ex_all["total_excluded"] == 3
    assert ex_all["total_in_scope"] == 2


def test_monotonicity_lower_consensus_upper():
    """Verify that sensitivity bounds satisfy: lower <= consensus <= upper.

    Given that the relevant sets satisfy:
    relevant_lower ⊆ relevant_consensus ⊆ relevant_upper,
    the recall@k and MRR@k metrics must be monotonically non-decreasing.
    """
    labels_a = {
        # q1: A and B agree on primary P1 -> lower has {P1}, consensus {P1, S1}, upper {P1, S1, S2}
        "q1": {"support_request": True, "language": "en", "primary": "P1", "secondary": ["S1"], "relevant_set": ["P1", "S1"]},
        # q2: A and B disagree on primary -> lower has {}, consensus {P2}, upper {P2, P2_alt}
        "q2": {"support_request": True, "language": "en", "primary": "P2_alt", "secondary": [], "relevant_set": ["P2_alt"]},
        # q3: A and B agree on primary P3 -> lower {P3}, consensus {P3}, upper {P3, S3}
        "q3": {"support_request": True, "language": "en", "primary": "P3", "secondary": [], "relevant_set": ["P3"]},
    }
    labels_b = {
        "q1": {"support_request": True, "language": "en", "primary": "P1", "secondary": ["S2"], "relevant_set": ["P1", "S2"]},
        "q2": {"support_request": True, "language": "en", "primary": "P2_other", "secondary": [], "relevant_set": ["P2_other"]},
        "q3": {"support_request": True, "language": "en", "primary": "P3", "secondary": ["S3"], "relevant_set": ["P3", "S3"]},
    }
    labels_c = {
        "q1": {"support_request": True, "language": "en", "primary": "P1", "secondary": ["S1"], "relevant_set": ["P1", "S1"]},
        "q2": {"support_request": True, "language": "en", "primary": "P2_alt", "secondary": ["P2_other"], "relevant_set": ["P2_alt", "P2_other"]},
        "q3": {"support_request": True, "language": "en", "primary": "P3", "secondary": [], "relevant_set": ["P3"]},
    }

    # Various system retrieval scenarios
    test_runs = [
        # System 1: only finds secondary/alt doc for q1 and q2
        {"q1": ["S1", "X", "Y"], "q2": ["P2_alt", "X"], "q3": ["Z"]},
        # System 2: perfect retrieval of primaries
        {"q1": ["P1"], "q2": ["P2_alt"], "q3": ["P3"]},
        # System 3: empty or unhelpful retrieval
        {"q1": ["X"], "q2": ["Y"], "q3": ["Z"]},
    ]

    for sys_top10 in test_runs:
        eval_res = evaluate_multilabel(
            system_top10=sys_top10,
            labels_c=labels_c,
            labels_a=labels_a,
            labels_b=labels_b,
            scope="MAIN",
        )
        low = eval_res["lower"]
        cons = eval_res["consensus"]
        up = eval_res["upper"]

        # Invariant: lower <= consensus <= upper for recall@5
        assert low["recall@5"] <= cons["recall@5"] + 1e-9, f"Recall violation: lower {low} > cons {cons}"
        assert cons["recall@5"] <= up["recall@5"] + 1e-9, f"Recall violation: cons {cons} > upper {up}"

        # Invariant: lower <= consensus <= upper for MRR@10
        assert low["mrr@10"] <= cons["mrr@10"] + 1e-9, f"MRR violation: lower {low} > cons {cons}"
        assert cons["mrr@10"] <= up["mrr@10"] + 1e-9, f"MRR violation: cons {cons} > upper {up}"


def test_load_system_rows(tmp_path):
    """Verify loading system rows from various JSON formats."""
    # Format 1: ablation_eval output with results array
    ablation_out = tmp_path / "ablation_out.json"
    ablation_data = {
        "timestamp": "2026-10-24T00:00:00Z",
        "results": [
            {
                "config": "baseline",
                "rows": [
                    {"qid": "q1", "top10": ["d1", "d2"]},
                    {"qid": "q2", "top10": ["d3"]},
                ],
            },
            {
                "config": "intent_soft_noisy_0.2",
                "rows": [
                    {"qid": "q1", "top10": ["d1", "d4"]},
                    {"qid": "q2", "top10": ["d3", "d5"]},
                ],
            },
        ],
    }
    ablation_out.write_text(json.dumps(ablation_data), encoding="utf-8")

    # Select specific config
    r_baseline = load_system_rows(str(ablation_out), config_name="baseline")
    assert r_baseline["q1"] == ["d1", "d2"]

    r_intent = load_system_rows(str(ablation_out), config_name="intent_soft_noisy_0.2")
    assert r_intent["q1"] == ["d1", "d4"]

    # Default prefers intent_soft_noisy_0.2
    r_def = load_system_rows(str(ablation_out))
    assert r_def["q1"] == ["d1", "d4"]

    # Format 2: Direct qid -> list mapping
    direct_out = tmp_path / "direct.json"
    direct_out.write_text(json.dumps({"q1": ["doc-A", "doc-B"]}), encoding="utf-8")
    r_direct = load_system_rows(str(direct_out))
    assert r_direct["q1"] == ["doc-A", "doc-B"]


def test_end_to_end_multilabel_eval_and_report():
    """Verify complete evaluation pipeline and report formatting."""
    labels_a = {
        "q1": {"support_request": True, "language": "en", "primary": "faq-order-status-track", "secondary": [], "relevant_set": ["faq-order-status-track"]},
        "q2": {"support_request": True, "language": "en", "primary": "faq-returns-refunds-window", "secondary": ["faq-returns-refunds-fees"], "relevant_set": ["faq-returns-refunds-window", "faq-returns-refunds-fees"]},
    }
    labels_b = {
        "q1": {"support_request": True, "language": "en", "primary": "faq-order-status-track", "secondary": [], "relevant_set": ["faq-order-status-track"]},
        "q2": {"support_request": True, "language": "en", "primary": "faq-returns-refunds-window", "secondary": [], "relevant_set": ["faq-returns-refunds-window"]},
    }
    labels_c = {
        "q1": {"support_request": True, "language": "en", "primary": "faq-order-status-track", "secondary": [], "relevant_set": ["faq-order-status-track"]},
        "q2": {"support_request": True, "language": "en", "primary": "faq-returns-refunds-window", "secondary": ["faq-returns-refunds-fees"], "relevant_set": ["faq-returns-refunds-window", "faq-returns-refunds-fees"]},
    }
    sys_top10 = {
        "q1": ["faq-order-status-track", "other-1"],
        "q2": ["faq-returns-refunds-fees", "other-2"],  # hits secondary doc
    }

    eval_res = evaluate_multilabel(
        system_top10=sys_top10,
        labels_c=labels_c,
        labels_a=labels_a,
        labels_b=labels_b,
        scope="MAIN",
    )
    agreement = compute_agreement_stats(labels_a, labels_b)

    assert eval_res["n_evaluated"] == 2
    assert eval_res["consensus"]["recall@5"] == 1.0
    assert eval_res["by_intent"]["order_status"]["recall@5"] == 1.0
    assert eval_res["by_intent"]["returns_refunds"]["recall@5"] == 1.0

    report = generate_evaluation_report(eval_res, agreement)
    assert "# PLAN4 Multi-Relevant Retrieval Evaluation Report" in report
    assert "Inter-Annotator Agreement" in report
    assert "Consensus Retrieval by Intent Family" in report
    assert "PLAN4 Decision Rule Summary" in report


def test_mixed_split_labels_with_test_only_run():
    """Verify that a mixed-split label file scored against a TEST-only system run scores exactly the test queries."""
    labels_c = {
        # TEST queries (t prefix)
        "t001": {"support_request": True, "language": "en", "primary": "doc-1", "secondary": [], "relevant_set": ["doc-1"]},
        "t002": {"support_request": True, "language": "en", "primary": "doc-2", "secondary": [], "relevant_set": ["doc-2"]},
        "t003": {"support_request": False, "language": "en", "primary": None, "secondary": [], "relevant_set": []},  # excluded
        # DEV queries (d prefix)
        "d001": {"support_request": True, "language": "en", "primary": "doc-3", "secondary": [], "relevant_set": ["doc-3"]},
        "d002": {"support_request": True, "language": "en", "primary": "doc-4", "secondary": [], "relevant_set": ["doc-4"]},
    }
    sys_top10 = {
        "t001": ["doc-1"],
        "t002": ["doc-2"],
    }

    res = evaluate_multilabel(
        system_top10=sys_top10,
        labels_c=labels_c,
        split="test",
        scope="MAIN",
    )
    assert res["split"] == "test"
    assert res["n_evaluated"] == 2
    assert res["exclusions"]["total_evaluated"] == 3  # 3 test queries evaluated
    assert res["exclusions"]["total_in_scope"] == 2
    assert res["exclusions"]["not_support_request"] == 1
    assert res["consensus"]["recall@5"] == 1.0


def test_missing_in_scope_system_row_raises():
    """Verify that if any in-scope labeled query in the split lacks a system row, an error is raised."""
    labels_c = {
        "t001": {"support_request": True, "language": "en", "primary": "doc-1", "secondary": [], "relevant_set": ["doc-1"]},
        "t002": {"support_request": True, "language": "en", "primary": "doc-2", "secondary": [], "relevant_set": ["doc-2"]},
    }
    # System run missing t002
    sys_top10 = {
        "t001": ["doc-1"],
    }

    with pytest.raises(ValueError, match="missing from the system run"):
        evaluate_multilabel(
            system_top10=sys_top10,
            labels_c=labels_c,
            split="test",
            scope="MAIN",
        )

