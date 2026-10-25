"""tests/rag/test_rerank_ce.py — Unit tests for cross-encoder reranker logic and IO schema.

Covers:
1. Graded target label normalization (grade_to_label: 2->1.0, 1->0.5, 0->0.0).
2. Rank prior computation (reciprocal and linear modes, bounds, and monotonicity).
3. Score fusion math across alpha values in [0.0, 1.0].
4. Candidate reranking, ordering, and deterministic tie-breaking.
5. Multi-alpha dataset reranking and IO schema conformance (matching dev_rows_phaseD.json).
6. Docs catalog and candidates parsing with temp fixtures.

Offline mode: uses mock scorers and stubs; no network or model downloads.

# REQ-RAG-RETR-016
"""

from datetime import datetime
import json
from pathlib import Path
import pytest

from rag.rerank_ce import (
    compute_rank_prior,
    fuse_scores,
    grade_to_label,
    load_candidates_data,
    load_docs,
    rerank_candidates,
    rerank_dataset,
)


def _mock_score_descending(pairs):
    """Scorer that assigns doc_C > doc_A > doc_B."""
    scores_by_text = {"Text A": 0.3, "Text B": 0.1, "Text C": 0.9}
    return [scores_by_text.get(p[1], 0.0) for p in pairs]


def _mock_score_ascending(pairs):
    """Scorer assigning ascending scores [0.1, 0.5, 0.9]."""
    values = [0.1, 0.5, 0.9]
    return values[: len(pairs)]


def _mock_score_tradeoff(pairs):
    """Scorer for testing alpha tradeoff."""
    return [0.4, 0.8][: len(pairs)]


def _mock_score_tradeoff_high(pairs):
    """Scorer for testing high alpha tradeoff."""
    return [0.4, 0.8][: len(pairs)]


def _mock_score_ties(pairs):
    """Scorer assigning identical scores for tie-breaking check."""
    return [0.7] * len(pairs)


def _mock_score_empty(pairs):
    """Scorer for empty input."""
    return []


def _mock_score_constant_half(pairs):
    """Scorer assigning 0.5 to all pairs."""
    return [0.5] * len(pairs)


def test_grade_to_label_mapping():
    """Verify grade normalization: 2->1.0 (primary), 1->0.5 (secondary), 0->0.0 (negative)."""
    assert grade_to_label(2) == 1.0
    assert grade_to_label(1) == 0.5
    assert grade_to_label(0) == 0.0

    # Test clamping
    assert grade_to_label(3) == 1.0
    assert grade_to_label(-1) == 0.0
    assert grade_to_label(1.5) == 0.75


def test_compute_rank_prior_reciprocal():
    """Verify reciprocal rank prior: 1 / (rank + 1)."""
    assert compute_rank_prior(0, total=10, mode="reciprocal") == 1.0
    assert compute_rank_prior(1, total=10, mode="reciprocal") == 0.5
    assert pytest.approx(compute_rank_prior(2, total=10, mode="reciprocal")) == 1.0 / 3.0
    assert pytest.approx(compute_rank_prior(9, total=10, mode="reciprocal")) == 0.1


def test_compute_rank_prior_linear():
    """Verify linear rank prior: (total - rank) / total."""
    assert compute_rank_prior(0, total=10, mode="linear") == 1.0
    assert pytest.approx(compute_rank_prior(1, total=10, mode="linear")) == 0.9
    assert pytest.approx(compute_rank_prior(5, total=10, mode="linear")) == 0.5
    assert pytest.approx(compute_rank_prior(9, total=10, mode="linear")) == 0.1


def test_compute_rank_prior_monotonicity_and_bounds():
    """Verify rank prior strictly decreases and stays bounded in (0, 1]."""
    for mode in ["reciprocal", "linear"]:
        priors = [compute_rank_prior(r, total=10, mode=mode) for r in range(10)]
        for i in range(len(priors) - 1):
            assert priors[i] > priors[i + 1], f"Mode {mode} failed strict descent at rank {i}"
        assert priors[0] == 1.0
        assert all(0.0 < p <= 1.0 for p in priors)


def test_compute_rank_prior_invalid_inputs():
    """Verify invalid inputs raise ValueError."""
    with pytest.raises(ValueError, match="rank must be non-negative"):
        compute_rank_prior(-1, total=10)

    with pytest.raises(ValueError, match="total must be positive"):
        compute_rank_prior(0, total=0)

    with pytest.raises(ValueError, match="Unsupported prior mode"):
        compute_rank_prior(0, total=10, mode="invalid_mode")


def test_fuse_scores_boundary_alphas():
    """Verify score fusion behavior at boundary alphas."""
    ce = 0.85
    rank = 3
    prior = compute_rank_prior(rank, total=10, mode="reciprocal")  # 1/4 = 0.25

    # Alpha = 1.0: Pure cross-encoder score
    assert fuse_scores(ce, rank=rank, alpha=1.0, total=10) == ce

    # Alpha = 0.0: Pure rank prior score
    assert pytest.approx(fuse_scores(ce, rank=rank, alpha=0.0, total=10)) == prior

    # Alpha = 0.5: Exact average
    expected = 0.5 * ce + 0.5 * prior
    assert pytest.approx(fuse_scores(ce, rank=rank, alpha=0.5, total=10)) == expected


def test_fuse_scores_invalid_alpha():
    """Verify alpha outside [0, 1] raises ValueError."""
    with pytest.raises(ValueError, match="alpha must be in"):
        fuse_scores(0.5, rank=0, alpha=-0.1)

    with pytest.raises(ValueError, match="alpha must be in"):
        fuse_scores(0.5, rank=0, alpha=1.1)


def test_rerank_candidates_pure_ce():
    """Verify alpha=1.0 reorders candidates strictly by cross-encoder score."""
    candidates = ["doc_A", "doc_B", "doc_C"]
    docs_map = {"doc_A": "Text A", "doc_B": "Text B", "doc_C": "Text C"}

    reranked = rerank_candidates(
        query="test query",
        candidate_ids=candidates,
        docs_map=docs_map,
        scorer=_mock_score_descending,
        alpha=1.0,
    )

    reranked_ids = [doc_id for doc_id, _ in reranked]
    assert reranked_ids == ["doc_C", "doc_A", "doc_B"]


def test_rerank_candidates_pure_prior():
    """Verify alpha=0.0 preserves original retrieval rank order regardless of CE scores."""
    candidates = ["doc_A", "doc_B", "doc_C"]
    docs_map = {"doc_A": "Text A", "doc_B": "Text B", "doc_C": "Text C"}

    reranked = rerank_candidates(
        query="test query",
        candidate_ids=candidates,
        docs_map=docs_map,
        scorer=_mock_score_ascending,
        alpha=0.0,
    )

    reranked_ids = [doc_id for doc_id, _ in reranked]
    assert reranked_ids == ["doc_A", "doc_B", "doc_C"]


def test_rerank_candidates_blended_balance():
    """Verify blended alpha combines rank prior with CE score."""
    candidates = ["doc_A", "doc_B"]
    docs_map = {"doc_A": "Text A", "doc_B": "Text B"}

    # doc_A: rank 0 (prior=1.0), CE=0.4
    # doc_B: rank 1 (prior=0.5), CE=0.8
    # alpha=0.5: doc_A fused = 0.5*0.4 + 0.5*1.0 = 0.70; doc_B fused = 0.5*0.8 + 0.5*0.5 = 0.65 -> doc_A wins!
    # alpha=0.8: doc_A fused = 0.8*0.4 + 0.2*1.0 = 0.52; doc_B fused = 0.8*0.8 + 0.2*0.5 = 0.74 -> doc_B wins!
    res_a05 = rerank_candidates("q", candidates, docs_map, _mock_score_tradeoff, alpha=0.5)
    assert [d for d, _ in res_a05] == ["doc_A", "doc_B"]

    res_a08 = rerank_candidates("q", candidates, docs_map, _mock_score_tradeoff_high, alpha=0.8)
    assert [d for d, _ in res_a08] == ["doc_B", "doc_A"]


def test_rerank_candidates_tie_breaking():
    """Verify identical fused scores deterministically preserve original candidate ordering."""
    candidates = ["doc_1", "doc_2", "doc_3"]
    docs_map = {cid: cid for cid in candidates}

    reranked = rerank_candidates("q", candidates, docs_map, _mock_score_ties, alpha=1.0)
    assert [d for d, _ in reranked] == ["doc_1", "doc_2", "doc_3"]


def test_rerank_candidates_empty():
    """Verify empty candidates returns empty list."""
    res = rerank_candidates("q", [], {}, _mock_score_empty)
    assert res == []


def test_rerank_dataset_io_schema():
    """Verify rerank_dataset output adheres to dev_rows_phaseD.json schema."""
    candidate_rows = [
        {"qid": "d001", "query": "first query", "top10": ["doc_1", "doc_2"]},
        {"qid": "d002", "query": "second query", "top10": ["doc_3", "doc_4"]},
        {"qid": "d003", "query": "empty query", "top10": []},
    ]
    docs_map = {"doc_1": "text 1", "doc_2": "text 2", "doc_3": "text 3", "doc_4": "text 4"}

    alphas = (0.3, 0.5, 0.7, 1.0)
    out = rerank_dataset(
        candidate_rows=candidate_rows,
        docs_map=docs_map,
        scorer=_mock_score_constant_half,
        alphas=alphas,
        prior_mode="reciprocal",
        config_prefix="ce_minilm",
        model_name="mock-model",
    )

    # Top-level schema
    assert "timestamp" in out
    assert out["model_name"] == "mock-model"
    assert out["n_queries"] == 3
    assert "results" in out
    assert len(out["results"]) == 4

    # Result structure per alpha
    for idx, alpha in enumerate(alphas):
        res = out["results"][idx]
        assert res["config"] == f"ce_minilm_a{alpha}"
        assert res["alpha"] == alpha
        assert res["prior_mode"] == "reciprocal"
        assert res["status"] == "completed"

        lat = res["latency_summary"]
        assert lat["count"] == 3
        assert "p50_ms" in lat
        assert "p95_ms" in lat
        assert "mean_ms" in lat

        rows = res["rows"]
        assert len(rows) == 3
        assert rows[0]["qid"] == "d001"
        assert isinstance(rows[0]["top10_ids"], list)
        assert rows[0]["top10_ids"] == ["doc_1", "doc_2"]
        assert rows[2]["qid"] == "d003"
        assert rows[2]["top10_ids"] == []


def test_load_docs_and_candidates(tmp_path: Path):
    """Verify load_docs and load_candidates_data on local test files."""
    docs_file = tmp_path / "docs.json"
    docs_data = {
        "doc_X": {"title": "Title X", "text": "Full text X"},
        "doc_Y": {"title": "Title Y"},
    }
    with open(docs_file, "w", encoding="utf-8") as f:
        json.dump(docs_data, f)

    docs_map = load_docs(docs_file)
    assert docs_map["doc_X"] == "Full text X"
    assert docs_map["doc_Y"] == "Title Y"

    cands_file = tmp_path / "candidates.jsonl"
    lines = [
        {"qid": "q1", "query": "hello", "top10": ["doc_X"]},
        {"qid": "q2", "query": "world", "top10": ["doc_Y"]},
    ]
    with open(cands_file, "w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    loaded = load_candidates_data(cands_file)
    assert len(loaded) == 2
    assert loaded[0]["qid"] == "q1"
    assert loaded[1]["top10"] == ["doc_Y"]
