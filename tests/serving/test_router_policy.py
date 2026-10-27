# REQ-SERVE-002: Router Long-Input Policy and Chunk Aggregation
# REQ-SERVE-003: Router Temperature Scaling and Decision Taxonomy
# REQ-SERVE-004: Choice Reranker Candidate Scoring and Sorting
"""tests/serving/test_router_policy.py — Pure-Python unit tests for router and reranker policy logic.

Zero PyTorch/GPU dependency: validates text cleaning, chunking, temperature scaling,
chunk aggregation (max for escalate/urgency, mean for sentiment/intent), and reranking.
"""

import pytest

from serving.laya.policy import (
    DEFAULT_ROUTER_TEMPERATURE,
    aggregate_chunk_decisions,
    apply_temperature_scaling,
    build_rerank_choice_question,
    chunk_text,
    clean_text,
    extract_raw_probabilities,
    format_rerank_response,
    format_single_chunk_decision,
)


# REQ-SERVE-002
def test_clean_text():
    """Verify clean_text strips extraneous newlines, tabs, and duplicate spaces."""
    assert clean_text("") == ""
    assert clean_text(None) == ""
    raw = "  Hello \n\n world! \t How   are you?\r\n"
    assert clean_text(raw) == "Hello world! How are you?"


# REQ-SERVE-002
def test_chunk_text_short():
    """Short text within max_chars should return a single chunk."""
    short = "My order #12345 has not arrived."
    chunks = chunk_text(short, max_chars=1800)
    assert len(chunks) == 1
    assert chunks[0] == short


# REQ-SERVE-002
def test_chunk_text_long_input_policy():
    """Long text exceeding max_chars should produce overlapping chunks."""
    sentence = "This is a detailed customer issue report with repeated context. "
    long_text = sentence * 40  # ~2,560 characters
    chunks = chunk_text(long_text, max_chars=1000, overlap_chars=200)

    assert len(chunks) >= 3
    for c in chunks:
        assert len(c) <= 1000
    # Overlap verification: end of chunk 0 should overlap start of chunk 1
    tail_chunk0 = chunks[0][-100:]
    assert tail_chunk0 in chunks[1] or any(word in chunks[1] for word in tail_chunk0.split()[:3])


# REQ-SERVE-003
def test_apply_temperature_scaling_neutral():
    """T=1.0 scaling should preserve probabilities."""
    probs = {"angry": 0.80, "neutral": 0.20}
    scaled = apply_temperature_scaling(probs, temperature=1.0)
    assert scaled["angry"] == 0.80
    assert scaled["neutral"] == 0.20


# REQ-SERVE-003
def test_apply_temperature_scaling_softening():
    """T=1.18 should soften peak probabilities while summing to 1.0."""
    probs = {"order_status": 0.90, "shipping_policy": 0.10}
    scaled = apply_temperature_scaling(probs, temperature=DEFAULT_ROUTER_TEMPERATURE)

    # Top probability should be softened (lower than 0.90)
    assert scaled["order_status"] < 0.90
    assert scaled["shipping_policy"] > 0.10
    assert pytest.approx(sum(scaled.values()), abs=1e-3) == 1.0


# REQ-SERVE-003
def test_extract_raw_probabilities():
    """Verify raw answer probability extraction across noul and dict shapes."""
    # Boolean noul format
    noul_ans = {"noul": 0.85}
    esc_p = extract_raw_probabilities(noul_ans)
    assert esc_p["true"] == 0.85
    assert esc_p["false"] == 0.15

    # Direct probabilities dict
    choice_ans = {"probabilities": {"order_status": 0.70, "other": 0.30}}
    probs = extract_raw_probabilities(choice_ans)
    assert probs["order_status"] == 0.70


# REQ-SERVE-003
def test_format_single_chunk_decision():
    """Verify format_single_chunk_decision parses all 4 router questions."""
    raw = {
        "escalate": {"noul": 0.95},
        "sentiment": {"probabilities": {"angry": 0.85, "neutral": 0.15}},
        "urgency": {"probabilities": {"0": 0.05, "1": 0.10, "2": 0.15, "3": 0.70}},
        "intent": {"probabilities": {"order_status": 0.92, "other": 0.08}},
    }
    decision = format_single_chunk_decision(raw, temperature=1.18)

    assert decision["escalate"]["label"] == "escalate"
    assert decision["escalate"]["prob"] > 0.80
    assert decision["sentiment"]["label"] == "angry"
    assert decision["urgency"]["level"] == 3
    assert decision["urgency"]["label"] == "immediate"
    assert decision["intent"]["label"] == "order_status"


# REQ-SERVE-002, REQ-SERVE-003
def test_aggregate_chunk_decisions_max_and_mean():
    """Verify chunk aggregation: max pooling for escalate/urgency, mean for sentiment/intent."""
    chunk1 = {
        "escalate": {"label": "do_not_escalate", "prob": 0.20, "probabilities": {"false": 0.80, "true": 0.20}},
        "sentiment": {"label": "neutral", "prob": 0.70, "probabilities": {"neutral": 0.70, "angry": 0.30}},
        "urgency": {"level": 1, "label": "within a day", "prob": 0.80, "probabilities": {"1": 0.80}},
        "intent": {"label": "order_status", "prob": 0.80, "probabilities": {"order_status": 0.80, "shipping_policy": 0.20}},
    }
    chunk2 = {
        # High escalation spike in second chunk
        "escalate": {"label": "escalate", "prob": 0.95, "probabilities": {"false": 0.05, "true": 0.95}},
        "sentiment": {"label": "angry", "prob": 0.80, "probabilities": {"neutral": 0.20, "angry": 0.80}},
        "urgency": {"level": 3, "label": "immediate", "prob": 0.90, "probabilities": {"3": 0.90}},
        "intent": {"label": "order_status", "prob": 0.60, "probabilities": {"order_status": 0.60, "shipping_policy": 0.40}},
    }

    agg = aggregate_chunk_decisions([chunk1, chunk2], model_version="v2", latency_ms=12.5)

    # 1. Escalate: max pooling should pick 0.95 -> escalate
    assert agg["escalate"]["label"] == "escalate"
    assert agg["escalate"]["prob"] == 0.95

    # 2. Urgency: max pooling should pick level 3
    assert agg["urgency"]["level"] == 3
    assert agg["urgency"]["label"] == "immediate"

    # 3. Sentiment: mean pooling ((0.70+0.20)/2 = 0.45 neutral, (0.30+0.80)/2 = 0.55 angry) -> angry wins
    assert agg["sentiment"]["label"] == "angry"
    assert pytest.approx(agg["sentiment"]["probabilities"]["angry"], abs=1e-2) == 0.55

    # 4. Intent: mean pooling ((0.80+0.60)/2 = 0.70 order_status)
    assert agg["intent"]["label"] == "order_status"
    assert pytest.approx(agg["intent"]["probabilities"]["order_status"], abs=1e-2) == 0.70

    assert agg["model_version"] == "v2"
    assert agg["latency_ms"] == 12.5


# REQ-SERVE-004
def test_build_rerank_choice_question():
    """Verify build_rerank_choice_question formats candidate criteria."""
    cand_ids = ["faq_1", "faq_2"]
    cand_texts = {"faq_1": "Return guidelines", "faq_2": "Shipping rates"}
    q = build_rerank_choice_question(cand_ids, cand_texts)

    assert q["type"] == "choice"
    assert "instructions" in q
    assert q["criteria"]["faq_1"] == "Return guidelines"
    assert q["criteria"]["faq_2"] == "Shipping rates"


# REQ-SERVE-004
def test_format_rerank_response_sorting_and_tail():
    """Verify format_rerank_response sorts top 5 by score and preserves tail."""
    candidates = [
        {"id": "c1", "text": "Doc 1"},
        {"id": "c2", "text": "Doc 2"},
        {"id": "c3", "text": "Doc 3"},
        {"id": "c4", "text": "Doc 4"},
        {"id": "c5", "text": "Doc 5"},
        {"id": "c6", "text": "Doc 6 (tail)"},
        {"id": "c7", "text": "Doc 7 (tail)"},
    ]
    # c3 has highest probability, followed by c1
    choice_probs = {
        "c1": 0.25,
        "c2": 0.05,
        "c3": 0.60,
        "c4": 0.08,
        "c5": 0.02,
    }

    ranked = format_rerank_response(candidates, choice_probs, latency_ms=18.42)
    assert len(ranked) == 7
    assert ranked[0]["latency_ms"] == 18.42

    # Top 5 reordered by score: c3 (0.60), c1 (0.25), c4 (0.08), c2 (0.05), c5 (0.02)
    ranked_ids = [r["id"] for r in ranked]
    assert ranked_ids[:5] == ["c3", "c1", "c4", "c2", "c5"]

    # Tail candidates preserved in relative order at ranks 6 and 7
    assert ranked_ids[5:] == ["c6", "c7"]

    # Monotonic descending score check
    for i in range(len(ranked) - 1):
        assert ranked[i]["score"] >= ranked[i + 1]["score"]
