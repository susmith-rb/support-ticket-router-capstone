# REQ-RAG-RETR-002: Offline Retrieval Metrics Computation
# REQ-RAG-RETR-013: Evaluation Set Split and Metric Standards
"""Unit tests for pure evaluation metrics in rag/eval_metrics.py."""

import math

import pytest

from evals.eval_metrics import (
    compute_metrics_summary,
    compute_rank,
    filter_slice,
    get_dropped_and_merged_ids,
    latency_percentiles,
    mrr_at_k,
    ndcg_at_k,
    ndcg_for_rank,
    recall_at_k,
)


def test_compute_rank():
    # REQ-RAG-RETR-002
    retrieved = ["doc-a", "doc-b", "doc-c", "doc-d"]

    assert compute_rank("doc-a", retrieved) == 1
    assert compute_rank("doc-c", retrieved) == 3
    assert compute_rank("doc-z", retrieved) is None

    # Multi-target returns earliest rank
    assert compute_rank(["doc-z", "doc-b", "doc-d"], retrieved) == 2
    assert compute_rank({"doc-c", "doc-d"}, retrieved) == 3
    assert compute_rank(["doc-x", "doc-y"], retrieved) is None


def test_recall_at_k():
    # REQ-RAG-RETR-002
    ranks = [1, 2, 4, 6, None, 10, None]
    # n = 7
    # <= 1: [1] -> 1/7
    assert recall_at_k(ranks, 1) == pytest.approx(1 / 7)
    # <= 3: [1, 2] -> 2/7
    assert recall_at_k(ranks, 3) == pytest.approx(2 / 7)
    # <= 5: [1, 2, 4] -> 3/7
    assert recall_at_k(ranks, 5) == pytest.approx(3 / 7)
    # <= 10: [1, 2, 4, 6, 10] -> 5/7
    assert recall_at_k(ranks, 10) == pytest.approx(5 / 7)

    # Custom n
    assert recall_at_k(ranks, 5, n=10) == pytest.approx(3 / 10)
    assert recall_at_k([], 5) == 0.0
    assert recall_at_k([1, 2], 5, n=0) == 0.0


def test_mrr_at_k():
    # REQ-RAG-RETR-002
    ranks = [1, 2, 4, None, 12]
    # n = 5, k = 10
    # rank 1: 1.0, rank 2: 0.5, rank 4: 0.25, None: 0, rank 12 (> 10): 0.0
    expected_sum = 1.0 + 0.5 + 0.25 + 0.0 + 0.0
    assert mrr_at_k(ranks, k=10) == pytest.approx(expected_sum / 5)

    # Empty list
    assert mrr_at_k([], k=10) == 0.0
    assert mrr_at_k([1], k=10, n=0) == 0.0


def test_ndcg_known_values():
    # REQ-RAG-RETR-013
    # Single-gold binary relevance:
    # rank 1 = 1 / log2(2) = 1.0
    assert ndcg_for_rank(1, k=5) == 1.0

    # rank 2 = 1 / log2(3)
    expected_rank_2 = 1.0 / math.log2(3)
    assert pytest.approx(ndcg_for_rank(2, k=5), rel=1e-6) == expected_rank_2

    # rank 3 = 1 / log2(4) = 0.5
    assert ndcg_for_rank(3, k=5) == 0.5

    # rank 4 = 1 / log2(5)
    assert pytest.approx(ndcg_for_rank(4, k=5), rel=1e-6) == 1.0 / math.log2(5)

    # rank 5 = 1 / log2(6)
    assert pytest.approx(ndcg_for_rank(5, k=5), rel=1e-6) == 1.0 / math.log2(6)

    # rank > k or None -> 0.0
    assert ndcg_for_rank(6, k=5) == 0.0
    assert ndcg_for_rank(10, k=5) == 0.0
    assert ndcg_for_rank(None, k=5) == 0.0
    assert ndcg_for_rank(0, k=5) == 0.0


def test_ndcg_at_k_mean():
    # REQ-RAG-RETR-013
    ranks = [1, 2, 3, None]
    # Sum: 1.0 + 1/log2(3) + 0.5 + 0.0 = 1.5 + 1/log2(3)
    expected_mean = (1.5 + (1.0 / math.log2(3))) / 4
    assert pytest.approx(ndcg_at_k(ranks, k=5), rel=1e-4) == expected_mean

    assert ndcg_at_k([], k=5) == 0.0
    assert ndcg_at_k([1, 2], k=5, n=0) == 0.0


def test_latency_percentiles():
    # REQ-RAG-RETR-013
    empty_res = latency_percentiles([])
    assert empty_res["count"] == 0
    assert empty_res["mean"] is None
    assert empty_res["p50"] is None

    data = [100.0, 200.0, 300.0, 400.0, 500.0]
    res = latency_percentiles(data)
    assert res["count"] == 5
    assert res["mean"] == 300.0
    assert res["min"] == 100.0
    assert res["max"] == 500.0
    assert res["p50"] == 300.0
    assert res["p90"] == 460.0
    assert res["p95"] == 480.0
    assert res["p99"] == 496.0


def test_slice_helper():
    # REQ-RAG-RETR-013
    gold_map = {
        "dropped": {"doc-drop-1": "reason", "doc-drop-2": "reason"},
        "map": {
            "doc-merge-1": "doc-merged-target",
            "doc-merge-2": "doc-merged-target",
            "doc-kept": "doc-kept",
        },
    }

    dropped, merged = get_dropped_and_merged_ids(gold_map)
    assert dropped == {"doc-drop-1", "doc-drop-2"}
    assert merged == {"doc-merge-1", "doc-merge-2"}

    rows = [
        {"qid": "1", "original_gold": "doc-kept", "relevant": "doc-kept"},
        {"qid": "2", "original_gold": "doc-drop-1", "relevant": "doc-drop-1"},
        {"qid": "3", "original_gold": "doc-merge-1", "relevant": "doc-merged-target"},
    ]

    slice_a = filter_slice(rows, gold_map, "a")
    assert len(slice_a) == 3

    slice_b = filter_slice(rows, gold_map, "b")
    assert len(slice_b) == 2
    assert [r["qid"] for r in slice_b] == ["1", "3"]

    slice_c = filter_slice(rows, gold_map, "c")
    assert len(slice_c) == 1
    assert [r["qid"] for r in slice_c] == ["1"]

    with pytest.raises(ValueError, match="Unknown slice_name"):
        filter_slice(rows, gold_map, "unknown_slice")


def test_compute_metrics_summary():
    # REQ-RAG-RETR-013
    rows = [
        {
            "qid": "t1",
            "intent": "order_status",
            "rank": 1,
            "latency_ms": 120.0,
            "top3": ["doc-a"],
        },
        {
            "qid": "t2",
            "intent": "order_status",
            "rank": 2,
            "latency_ms": 180.0,
            "top3": ["doc-x", "doc-b"],
        },
        {
            "qid": "t3",
            "intent": "account_payment",
            "rank": None,
            "latency_ms": 210.0,
            "top3": [],
        },
    ]

    summary = compute_metrics_summary(rows)
    assert summary["n"] == 3
    assert summary["zero_result_queries"] == 1
    assert summary["recall@1"] == pytest.approx(1 / 3)
    assert summary["recall@3"] == pytest.approx(2 / 3)
    assert summary["recall@5"] == pytest.approx(2 / 3)
    assert summary["mrr@10"] == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert summary["ndcg@5"] == pytest.approx((1.0 + (1.0 / math.log2(3)) + 0.0) / 3)

    assert "order_status" in summary["by_intent"]
    assert summary["by_intent"]["order_status"]["n"] == 2
    assert summary["by_intent"]["order_status"]["recall@1"] == 0.5
    assert summary["by_intent"]["order_status"]["recall@3"] == 1.0

    assert "latency_ms" in summary
    assert summary["latency_ms"]["count"] == 3
    assert summary["latency_ms"]["min"] == 120.0
    assert summary["latency_ms"]["max"] == 210.0
