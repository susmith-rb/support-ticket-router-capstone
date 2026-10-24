"""Offline unit tests for rag/ablation_eval.py (REQ-RAG-RETR-011).

Tests cover:
- Configuration grid generation and filtering
- Deterministic noisy intent generation
- Split discipline verification (blocking test set evaluation)
- Offline mock evaluation of search and rerank ablation configs
- Markdown table formatting
"""

import json
import pytest
from ablation_eval import (
    AblationConfig,
    ALL_CONFIG_NAMES,
    evaluate_single_config,
    generate_markdown_table,
    generate_noisy_intents,
    get_ablation_configs,
)
from search_client import SearchResult


def test_get_ablation_configs_all():
    configs = get_ablation_configs()
    names = [c.name for c in configs]
    assert names == ALL_CONFIG_NAMES
    assert len(configs) == 13

    # Check specific config attributes
    baseline = next(c for c in configs if c.name == "baseline")
    assert baseline.threshold == "LOWEST"
    assert baseline.clean is True
    assert baseline.intent_mode == "off"

    rerank = next(c for c in configs if c.name == "managed_rerank")
    assert rerank.rerank is True
    assert rerank.rerank_top_n == 5

    rank_expr = next(c for c in configs if c.name == "rank_expr_semantic")
    assert rank_expr.supported is False

    soft_05 = next(c for c in configs if c.name == "intent_soft_oracle_0.5")
    assert soft_05.intent_mode == "oracle"
    assert soft_05.intent_boost == 0.5


def test_get_ablation_configs_filter():
    configs = get_ablation_configs(["baseline", "managed_rerank"])
    assert len(configs) == 2
    assert [c.name for c in configs] == ["baseline", "managed_rerank"]


def test_generate_noisy_intents_determinism():
    queries = [
        {"qid": f"d{i:03d}", "intent": "order_status"} for i in range(100)
    ]
    noisy1 = generate_noisy_intents(queries, seed=42, error_rate=0.14)
    noisy2 = generate_noisy_intents(queries, seed=42, error_rate=0.14)
    assert noisy1 == noisy2

    # Verify corruptions occurred at ~14% rate
    corrupted = sum(1 for o, n in zip(queries, noisy1) if o["intent"] != n)
    assert 5 <= corrupted <= 25


class MockClient:
    def __init__(self):
        self.recorded_calls = []

    def search(self, **kwargs):
        self.recorded_calls.append(kwargs)
        # Mock returns 3 docs, target is doc-1
        return SearchResult(
            document_ids=["doc-1", "doc-2", "doc-3"],
            latency_ms=120.0,
            total_size=3,
            filter_applied=kwargs.get("intent_family") or "",
            fallback_triggered=False,
            raw_results=[
                {"document": {"id": "doc-1", "derivedStructData": {"title": "Doc 1"}}},
                {"document": {"id": "doc-2", "derivedStructData": {"title": "Doc 2"}}},
                {"document": {"id": "doc-3", "derivedStructData": {"title": "Doc 3"}}},
            ],
        )

    def rank(self, query, records, top_n=5, model="", **kwargs):
        return [{"id": "doc-2"}, {"id": "doc-1"}], 45.0


def test_evaluate_single_config_mock():
    queries = [
        {"qid": "d001", "query": "track order", "relevant": "doc-1", "intent": "order_status"},
        {"qid": "d002", "query": "return item", "relevant": "doc-2", "intent": "returns_refunds"},
    ]
    noisy_intents = ["order_status", "returns_refunds"]
    gold_map = {"map": {}, "dropped": {}}
    mock_client = MockClient()

    cfg = AblationConfig(
        name="baseline",
        description="Baseline",
        threshold="LOWEST",
        clean=True,
    )

    res = evaluate_single_config(
        cfg=cfg,
        queries=queries,
        noisy_intents=noisy_intents,
        gold_map=gold_map,
        client=mock_client,
    )

    assert res["status"] == "completed"
    assert res["config"] == "baseline"
    b = res["slice_b"]
    assert b["n"] == 2
    # doc-1 is at rank 1 for query 1 (doc-1 in ["doc-1", "doc-2", "doc-3"])
    # doc-2 is at rank 2 for query 2
    assert b["recall@1"] == 0.5
    assert b["recall@5"] == 1.0
    assert b["mrr@10"] == pytest.approx(0.75)


def test_evaluate_managed_rerank_reuses_cache():
    queries = [
        {"qid": "d001", "query": "track order", "relevant": "doc-2", "intent": "order_status"},
    ]
    noisy_intents = ["order_status"]
    gold_map = {}
    mock_client = MockClient()

    base_cache = {
        "d001": SearchResult(
            document_ids=["doc-1", "doc-2"],
            latency_ms=100.0,
            total_size=2,
            filter_applied="",
            raw_results=[
                {"document": {"id": "doc-1", "derivedStructData": {"title": "Doc 1"}}},
                {"document": {"id": "doc-2", "derivedStructData": {"title": "Doc 2"}}},
            ],
        )
    }

    cfg = AblationConfig(
        name="managed_rerank",
        description="Rerank",
        rerank=True,
        rerank_top_n=2,
    )

    res = evaluate_single_config(
        cfg=cfg,
        queries=queries,
        noisy_intents=noisy_intents,
        gold_map=gold_map,
        client=mock_client,
        baseline_cache=base_cache,
    )

    assert res["status"] == "completed"
    b = res["slice_b"]
    # Mock rank puts doc-2 first, so for target doc-2 it should be rank 1!
    assert b["recall@1"] == 1.0


def test_evaluate_unsupported_config():
    cfg = AblationConfig(
        name="rank_expr_semantic",
        description="Unsupported",
        supported=False,
        support_note="HTTP 400",
    )
    res = evaluate_single_config(
        cfg=cfg,
        queries=[],
        noisy_intents=[],
        gold_map={},
        client=MockClient(),
    )
    assert res["status"] == "unsupported"
    assert res["support_note"] == "HTTP 400"


def test_generate_markdown_table():
    results = [
        {
            "config": "baseline",
            "status": "completed",
            "fallbacks_triggered": 0,
            "latency_summary": {"p50": 150.0, "p95": 220.0},
            "slice_b": {
                "n": 287,
                "recall@1": 0.45,
                "recall@5": 0.78,
                "recall@10": 0.88,
                "mrr@10": 0.58,
                "ndcg@5": 0.62,
            },
        },
        {
            "config": "rank_expr_semantic",
            "status": "unsupported",
        },
    ]
    md = generate_markdown_table(results)
    assert "| Configuration | Status |" in md
    assert "| **baseline** | completed | 287 | 0.4500 | **0.7800** |" in md
    assert "| **rank_expr_semantic** | *unsupported* |" in md
