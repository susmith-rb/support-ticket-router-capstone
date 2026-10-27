"""Offline unit tests for rag/ablation_eval.py (REQ-RAG-RETR-011).

Tests cover:
- Configuration grid generation and filtering
- Deterministic noisy intent generation
- Split discipline verification (blocking test set evaluation)
- Offline mock evaluation of search and rerank ablation configs
- Markdown table formatting
"""

import pytest

from evals.ablation_eval import (
    ALL_CONFIG_NAMES,
    AblationConfig,
    evaluate_single_config,
    generate_markdown_table,
    generate_noisy_intents,
    get_ablation_configs,
    validate_split_and_config_guard,
)
from ticket_router.retrieval.search_client import SearchResult


def test_get_ablation_configs_all():
    configs = get_ablation_configs()
    names = [c.name for c in configs]
    assert names == ALL_CONFIG_NAMES
    assert len(configs) == len(ALL_CONFIG_NAMES)

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

    soft_01 = next(c for c in configs if c.name == "intent_soft_oracle_0.1")
    assert soft_01.intent_mode == "oracle"
    assert soft_01.intent_boost == 0.1

    soft_03 = next(c for c in configs if c.name == "intent_soft_oracle_0.3")
    assert soft_03.intent_mode == "oracle"
    assert soft_03.intent_boost == 0.3

    soft_05 = next(c for c in configs if c.name == "intent_soft_oracle_0.5")
    assert soft_05.intent_mode == "oracle"
    assert soft_05.intent_boost == 0.5

    # Phase D configs
    r_all_20 = next(c for c in configs if c.name == "rerank_all_20")
    assert r_all_20.rerank is True
    assert r_all_20.rerank_top_n == 20
    assert r_all_20.rerank_margin is None
    assert r_all_20.use_rich_records is True

    r_blend_05 = next(c for c in configs if c.name == "rerank_blend_0.05")
    assert r_blend_05.rerank is True
    assert r_blend_05.rerank_margin == 0.05

    rw_only = next(c for c in configs if c.name == "rewrite_only")
    assert rw_only.query_rewrite_mode == "rewrite_only"

    rw_plus_orig = next(c for c in configs if c.name == "rewrite_plus_original")
    assert rw_plus_orig.query_rewrite_mode == "rewrite_plus_original"


def test_validate_split_and_config_guard(tmp_path):
    audit_file = tmp_path / "test_runs.log"

    # 1. Dev set passes without final_test
    dev_queries = [{"qid": "d001", "query": "dev query"}]
    validate_split_and_config_guard(
        queries_path="scratch/rag/eval/dev.jsonl",
        queries=dev_queries,
        final_test=False,
    )

    # 2. Test set fails without final_test
    test_queries = [{"qid": "t001", "query": "test query"}]
    with pytest.raises(ValueError, match="SPLIT DISCIPLINE VIOLATION"):
        validate_split_and_config_guard(
            queries_path="scratch/rag/eval/test.jsonl",
            queries=test_queries,
            final_test=False,
        )

    # 3. Test set fails with final_test=True but without configs allow-list
    with pytest.raises(ValueError, match="requires an explicit config allow-list"):
        validate_split_and_config_guard(
            queries_path="scratch/rag/eval/test.jsonl",
            queries=test_queries,
            final_test=True,
            selected_configs=None,
        )

    # 4. Test set passes with final_test=True and configs allow-list, writes audit log
    validate_split_and_config_guard(
        queries_path="scratch/rag/eval/test.jsonl",
        queries=test_queries,
        final_test=True,
        selected_configs=["baseline", "intent_soft_noisy_0.2"],
        audit_log_path=str(audit_file),
    )
    assert audit_file.exists()
    log_content = audit_file.read_text(encoding="utf-8")
    assert "FINAL_TEST_RUN" in log_content
    assert "configs=baseline,intent_soft_noisy_0.2" in log_content


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

    # Check rows are recorded with required fields
    assert "rows" in res
    assert len(res["rows"]) == 2
    row0 = res["rows"][0]
    assert row0["qid"] == "d001"
    assert row0["query"] == "track order"
    assert row0["top10"] == ["doc-1", "doc-2", "doc-3"]
    assert row0["top10_ids"] == ["doc-1", "doc-2", "doc-3"]
    assert row0["latency"] == 120.0
    assert row0["applied_intent"] is None


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


# REQ-RAG-RETR-016
def test_evaluate_single_config_with_rewrite_and_rich_rerank():
    class MockClientWithRank:
        def __init__(self):
            self.last_query = ""

        def search(self, **kwargs):
            self.last_query = kwargs.get("query", "")
            return SearchResult(
                document_ids=["doc-1", "doc-2"],
                latency_ms=10.0,
                total_size=2,
                filter_applied="status: ANY(\"active\", \"draft\")",
                raw_results=[
                    {"document": {"id": "doc-1"}},
                    {"document": {"id": "doc-2"}},
                ],
            )

        def rank(self, **kwargs):
            # Rank doc-2 higher (score 0.90) than doc-1 (score 0.70)
            return [
                {"id": "doc-2", "score": 0.90},
                {"id": "doc-1", "score": 0.70},
            ], 5.0

    client = MockClientWithRank()
    queries = [{"qid": "d001", "query": "raw customer tweet @support", "relevant": "doc-2"}]
    noisy_intents = ["other"]
    rewrite_cache = {"d001": {"rewritten": "clean customer question"}}
    rich_records = {
        "doc-1": {"id": "doc-1", "title": "Doc 1", "content": "Rich content 1"},
        "doc-2": {"id": "doc-2", "title": "Doc 2", "content": "Rich content 2"},
    }

    # Case 1: query_rewrite_mode = "rewrite_only" + rerank_all_20
    cfg1 = AblationConfig(
        name="rewrite_only_rerank_all_20",
        description="Rewrite + Rerank All",
        query_rewrite_mode="rewrite_only",
        rerank=True,
        rerank_top_n=20,
    )
    res1 = evaluate_single_config(
        cfg=cfg1,
        queries=queries,
        noisy_intents=noisy_intents,
        gold_map={},
        client=client,
        rich_records=rich_records,
        rewrite_cache=rewrite_cache,
    )
    assert res1["status"] == "completed"
    assert client.last_query == "clean customer question"
    assert res1["rows"][0]["top10"] == ["doc-2", "doc-1"]
    assert res1["rows"][0]["rank"] == 1
    assert res1["rows"][0]["latency_ms"] == 15.0  # 10 search + 5 rank

    # Case 2: query_rewrite_mode = "rewrite_plus_original" + rerank_blend_0.25 (margin > 0.20 delta)
    cfg2 = AblationConfig(
        name="rewrite_blend",
        description="Rewrite + Blend Margin 0.25",
        query_rewrite_mode="rewrite_plus_original",
        rerank=True,
        rerank_margin=0.25,  # Delta is 0.90 - 0.70 = 0.20 <= 0.25 -> keep doc-1 at rank 1!
    )
    res2 = evaluate_single_config(
        cfg=cfg2,
        queries=queries,
        noisy_intents=noisy_intents,
        gold_map={},
        client=client,
        rich_records=rich_records,
        rewrite_cache=rewrite_cache,
    )
    assert res2["status"] == "completed"
    assert "clean customer question raw customer tweet" in client.last_query
    # Margin not exceeded: doc-1 stays top 1!
    assert res2["rows"][0]["top10"] == ["doc-1", "doc-2"]
    assert res2["rows"][0]["rank"] == 2
