# REQ-RAG-RETR-013: Evaluation Set Split and Metric Standards
"""Unit tests for split generation and disjointness in rag/build_eval_splits.py."""

from collections import Counter
import json
from pathlib import Path
import pytest

from build_eval_splits import (
    assert_disjointness,
    build_dev_split,
    build_test_split,
    extract_theme_candidates,
    write_jsonl,
)


def make_mock_candidate(idx: int, intent: str, thread_id: str, query: str):
    return {
        "thread_id": thread_id,
        "query": query,
        "original_gold": f"faq-{intent}-article",
        "relevant": f"faq-{intent}-article",
        "intent": intent,
    }


def test_splits_disjointness():
    # REQ-RAG-RETR-013
    test_split = [
        {"qid": "t000", "thread_id": "101", "query": "How do I return?", "relevant": "faq-ret", "intent": "ret", "split": "test"},
        {"qid": "t001", "thread_id": "102", "query": "Where is my order?", "relevant": "faq-ord", "intent": "ord", "split": "test"},
    ]
    dev_split = [
        {"qid": "d000", "thread_id": "201", "query": "Account locked", "relevant": "faq-acc", "intent": "acc", "split": "dev"},
        {"qid": "d001", "thread_id": "202", "query": "Late shipment", "relevant": "faq-shp", "intent": "shp", "split": "dev"},
    ]
    examples = {"901", "902"}

    # Clean case should pass without error
    assert_disjointness(test_split, dev_split, examples)

    # Overlapping thread_id raises AssertionError
    bad_dev_tid = [
        {"qid": "d000", "thread_id": "101", "query": "Different text", "relevant": "faq-acc", "intent": "acc", "split": "dev"},
    ]
    with pytest.raises(AssertionError, match="thread_ids in BOTH dev and test"):
        assert_disjointness(test_split, bad_dev_tid, examples)

    # Overlapping query text raises AssertionError
    bad_dev_q = [
        {"qid": "d000", "thread_id": "301", "query": "How do I return?", "relevant": "faq-acc", "intent": "acc", "split": "dev"},
    ]
    with pytest.raises(AssertionError, match="queries in BOTH dev and test"):
        assert_disjointness(test_split, bad_dev_q, examples)

    # Leakage into example threads raises AssertionError
    bad_dev_ex = [
        {"qid": "d000", "thread_id": "901", "query": "New question", "relevant": "faq-acc", "intent": "acc", "split": "dev"},
    ]
    with pytest.raises(AssertionError, match="DEV threads in theme examples"):
        assert_disjointness(test_split, bad_dev_ex, examples)


def test_build_eval_splits_determinism():
    # REQ-RAG-RETR-013
    candidates = [
        make_mock_candidate(i, f"intent_{i % 3}", f"thread_{i}", f"query text {i}")
        for i in range(50)
    ]

    dev1 = build_dev_split(candidates, n_dev=10, seed=42)
    dev2 = build_dev_split(candidates, n_dev=10, seed=42)
    dev3 = build_dev_split(candidates, n_dev=10, seed=99)

    # Same seed produces exact same records in exact same order
    assert dev1 == dev2
    assert [d["qid"] for d in dev1] == [f"d{i:03d}" for i in range(10)]
    assert [d["thread_id"] for d in dev1] == [d["thread_id"] for d in dev2]

    # Different seed produces different order/selection
    assert [d["thread_id"] for d in dev1] != [d["thread_id"] for d in dev3]


def test_intent_mix_distribution():
    # REQ-RAG-RETR-013
    candidates = []
    # Create candidate pool with 5 intents
    for intent, count in [("returns_refunds", 40), ("order_status", 40), ("other", 40), ("account_payment", 30), ("shipping_policy", 20)]:
        for i in range(count):
            candidates.append(make_mock_candidate(len(candidates), intent, f"t_{intent}_{i}", f"Query {intent} {i}"))

    target_counts = {"returns_refunds": 10, "order_status": 8, "other": 8, "account_payment": 5, "shipping_policy": 2}
    stratified_dev = build_dev_split(candidates, n_dev=33, seed=42, stratified_counts=target_counts)
    actual_counts = Counter(d["intent"] for d in stratified_dev)
    assert actual_counts == target_counts


def test_eval_split_schema(tmp_path):
    # REQ-RAG-RETR-013
    candidates = [
        make_mock_candidate(i, "order_status", f"thread_{i}", f"Query text {i}")
        for i in range(10)
    ]
    dev = build_dev_split(candidates, n_dev=5, seed=42)
    out_file = tmp_path / "dev.jsonl"
    write_jsonl(dev, out_file)

    lines = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5

    required_keys = {"qid", "query", "relevant", "original_gold", "intent", "split", "thread_id"}
    for idx, item in enumerate(lines):
        assert set(item.keys()) == required_keys
        assert item["qid"] == f"d{idx:03d}"
        assert item["split"] == "dev"
        assert item["query"].startswith("Query text")
        assert item["intent"] == "order_status"


def test_live_eval_splits_if_present():
    # REQ-RAG-RETR-013
    # Check generated files in default scratch if they exist
    scratch_eval = Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/eval")
    test_path = scratch_eval / "test.jsonl"
    dev_path = scratch_eval / "dev.jsonl"

    if test_path.exists() and dev_path.exists():
        test_rows = [json.loads(l) for l in test_path.read_text(encoding="utf-8").splitlines()]
        dev_rows = [json.loads(l) for l in dev_path.read_text(encoding="utf-8").splitlines()]

        assert len(test_rows) == 300
        assert len(dev_rows) == 300

        # Disjoint thread IDs and queries
        test_tids = {r["thread_id"] for r in test_rows}
        dev_tids = {r["thread_id"] for r in dev_rows}
        assert not (test_tids & dev_tids)

        test_queries = {r["query"] for r in test_rows}
        dev_queries = {r["query"] for r in dev_rows}
        assert not (test_queries & dev_queries)
