# REQ-RAG-RETR-016: Query Rewriting and Rich Record Reranking for Graded Retrieval Precision
"""tests/rag/test_rerank_laya.py — Unit tests for Laya choice reranker (Arm B, PLAN7).

Validates:
1. Conversion schema: laya-train JSONL item structure (id, state, questions, gold, expected).
2. <= 5 options selection: respects Laya choice ceiling, includes all positives and hardest negatives.
3. Soft-gold normalization: primary=2, secondary=1, negatives=0 normalized to sum to 1.0.
4. Reranking inference logic: reorders top 5 by predicted probability, keeps ranks 6-10 untouched.
"""

import pytest
from rag.rerank_laya import (
    QUESTION_ID,
    convert_row_to_laya,
    format_option_text,
    format_scope_phrase,
    normalize_soft_gold,
    rerank_top5_candidates,
    select_options,
)


@pytest.fixture
def sample_docs():
    return {
        "doc_a": {
            "doc_id": "doc_a",
            "title": "Delivery Times and Speeds",
            "also_asked": ["When will my package arrive?"],
            "answer_prefix": "Standard delivery takes 3 to 5 business days.",
        },
        "doc_b": {
            "doc_id": "doc_b",
            "title": "Return Policy Overview",
            "also_asked": ["How do I return an item?"],
            "answer_prefix": "Items can be returned within 30 days of receipt.",
        },
        "doc_c": {
            "doc_id": "doc_c",
            "title": "Refund Status and Tracking",
            "also_asked": ["Where is my refund?"],
            "answer_prefix": "Refunds are processed within 5-7 business days.",
        },
        "doc_d": {
            "doc_id": "doc_d",
            "title": "Damaged or Defective Item",
            "also_asked": ["My package arrived broken."],
            "answer_prefix": "We will replace or refund damaged items immediately.",
        },
        "doc_e": {
            "doc_id": "doc_e",
            "title": "Account Login Issues",
            "also_asked": ["I cannot sign into my account."],
            "answer_prefix": "Reset your password using the forgot password link.",
        },
        "doc_f": {
            "doc_id": "doc_f",
            "title": "International Shipping Rates",
            "also_asked": ["Do you ship abroad?"],
            "answer_prefix": "International shipping rates vary by destination.",
        },
        "doc_g": {
            "doc_id": "doc_g",
            "title": "Gift Card Balance Inquiry",
            "also_asked": ["How to check balance?"],
            "answer_prefix": "View your gift card balance in account settings.",
        },
    }


def test_select_options_max_5():
    """Verify select_options selects at most 5 options with positives prioritized."""
    candidates = ["doc_a", "doc_b", "doc_c", "doc_d", "doc_e", "doc_f", "doc_g"]
    rels = {
        "doc_c": 2,  # positive (primary)
        "doc_f": 1,  # positive (secondary)
        "doc_a": 0,
        "doc_b": 0,
        "doc_d": 0,
        "doc_e": 0,
        "doc_g": 0,
    }

    selected = select_options(candidates, rels, max_options=5)
    assert len(selected) == 5
    # Positives must be included
    assert "doc_c" in selected
    assert "doc_f" in selected
    # First 3 hardest negatives from candidates list (doc_a, doc_b, doc_d)
    assert selected == ["doc_c", "doc_f", "doc_a", "doc_b", "doc_d"]


def test_select_options_fewer_than_5():
    """Verify select_options handles small candidate lists."""
    candidates = ["doc_a", "doc_b"]
    rels = {"doc_a": 2, "doc_b": 0}
    selected = select_options(candidates, rels, max_options=5)
    assert selected == ["doc_a", "doc_b"]


def test_select_options_many_positives():
    """Verify select_options caps at 5 even if there are >5 positives."""
    candidates = [f"doc_{i}" for i in range(8)]
    rels = {f"doc_{i}": 2 if i < 3 else 1 for i in range(8)}
    selected = select_options(candidates, rels, max_options=5)
    assert len(selected) == 5
    # Highest grades (grade 2) selected first
    assert set(selected[:3]) == {"doc_0", "doc_1", "doc_2"}


def test_normalize_soft_gold_primary_only():
    """Soft gold with single primary yields 1.0 for primary, 0.0 for others."""
    selected = ["doc_a", "doc_b", "doc_c"]
    rels = {"doc_b": 2, "doc_a": 0, "doc_c": 0}
    probs = normalize_soft_gold(selected, rels)
    assert probs["doc_b"] == 1.0
    assert probs["doc_a"] == 0.0
    assert probs["doc_c"] == 0.0
    assert pytest.approx(sum(probs.values()), abs=1e-5) == 1.0


def test_normalize_soft_gold_graded_multi_relevant():
    """Soft gold with primary (2) and secondary (1) produces 2/3 and 1/3."""
    selected = ["doc_a", "doc_b", "doc_c", "doc_d"]
    rels = {"doc_a": 2, "doc_c": 1, "doc_b": 0, "doc_d": 0}
    probs = normalize_soft_gold(selected, rels)
    assert pytest.approx(probs["doc_a"], abs=1e-4) == 2.0 / 3.0
    assert pytest.approx(probs["doc_c"], abs=1e-4) == 1.0 / 3.0
    assert probs["doc_b"] == 0.0
    assert probs["doc_d"] == 0.0
    assert pytest.approx(sum(probs.values()), abs=1e-5) == 1.0


def test_normalize_soft_gold_all_zeros_uniform():
    """If no positive exists in selected, soft gold falls back to uniform."""
    selected = ["doc_a", "doc_b", "doc_c"]
    rels = {"doc_a": 0, "doc_b": 0, "doc_c": 0}
    probs = normalize_soft_gold(selected, rels)
    for doc_id in selected:
        assert pytest.approx(probs[doc_id], abs=1e-4) == 1.0 / 3.0
    assert pytest.approx(sum(probs.values()), abs=1e-5) == 1.0


def test_format_option_text(sample_docs):
    """Verify format_option_text combines title and scope phrase cleanly."""
    opt_text = format_option_text("doc_a", sample_docs["doc_a"])
    assert "Delivery Times and Speeds" in opt_text
    assert "When will my package arrive?" in opt_text


def test_convert_row_to_laya_schema(sample_docs):
    """Verify convert_row_to_laya builds exact laya-train schema with <= 5 options."""
    row = {
        "qid": "test_001",
        "query": "Where is my package? It was supposed to be delivered yesterday.",
        "rels": {"doc_a": 2, "doc_c": 1, "doc_b": 0, "doc_d": 0, "doc_e": 0, "doc_f": 0},
        "candidates": ["doc_b", "doc_d", "doc_a", "doc_c", "doc_e", "doc_f"],
    }
    item = convert_row_to_laya(row, sample_docs, max_options=5)

    assert item["id"] == "test_001"
    assert item["state"] == row["query"]
    assert QUESTION_ID in item["questions"]
    q_def = item["questions"][QUESTION_ID]
    assert q_def["type"] == "choice"
    assert len(q_def["criteria"]) <= 5
    assert len(q_def["criteria"]) == 5

    # Check gold
    gold_probs = item["gold"][QUESTION_ID]["probabilities"]
    assert set(gold_probs.keys()) == set(q_def["criteria"].keys())
    assert pytest.approx(sum(gold_probs.values()), abs=1e-5) == 1.0
    assert item["expected"][QUESTION_ID] == "doc_a"


def _stub_predict(query: str, questions: dict) -> dict:
    """Stub model prediction for testing reranking logic.

    Gives highest prob to doc_c, then doc_e, then doc_a, then doc_b, then doc_d.
    """
    return {
        "answers": {
            QUESTION_ID: {
                "choice": "doc_c",
                "probabilities": {
                    "doc_a": 0.15,
                    "doc_b": 0.05,
                    "doc_c": 0.50,
                    "doc_d": 0.02,
                    "doc_e": 0.28,
                },
            }
        }
    }


def test_rerank_top5_candidates_with_stub():
    """Verify rerank_top5_candidates reorders top 5 by predicted prob and keeps ranks 6-10."""
    initial_top10 = [
        "doc_a",  # 0
        "doc_b",  # 1
        "doc_c",  # 2
        "doc_d",  # 3
        "doc_e",  # 4
        "doc_f",  # 5 (tail)
        "doc_g",  # 6 (tail)
    ]

    docs = {cid: {"title": f"Title {cid}", "answer_prefix": "Answer"} for cid in initial_top10}

    reordered, probs = rerank_top5_candidates(
        query="I need help with my refund status",
        top_candidates=initial_top10,
        agent_or_predict_fn=_stub_predict,
        docs=docs,
        k_rerank=5,
    )

    # Top 5 reordered by prob: doc_c (0.50), doc_e (0.28), doc_a (0.15), doc_b (0.05), doc_d (0.02)
    expected_top5 = ["doc_c", "doc_e", "doc_a", "doc_b", "doc_d"]
    assert reordered[:5] == expected_top5
    # Ranks 6-7 (doc_f, doc_g) must remain unchanged
    assert reordered[5:] == ["doc_f", "doc_g"]
    assert len(reordered) == len(initial_top10)
