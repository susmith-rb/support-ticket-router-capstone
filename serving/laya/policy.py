"""serving/laya/policy.py — Pure-Python request, response, and policy logic.

This module implements the long-input policy, temperature scaling, chunk aggregation,
and decision formatting for router-s1 and the Laya choice reranker.

Zero PyTorch dependency: this module contains pure Python logic to allow thorough
unit and integration testing without requiring GPU or PyTorch runtimes.

Logic adapted from:
- docs/TDD.md §2, §2.1, §3.6 (System-1 / System-2 routing, temperature scaling T=1.18, long-input policy)
- training/router_s1/pipeline/router_schema.py (Router question definitions and taxonomy)
- rag/rerank_laya.py (Choice question formatting and candidate sorting)
"""

import math
import re
from typing import Any, Dict, List, Optional, Sequence

# Router question definition (canonical contract from training/router_s1/pipeline/router_schema.py)
ROUTER_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "escalate": {
        "type": "noul",
        "instructions": "Should a human support agent handle this message immediately instead of an automated reply?",
        "criteria": {
            "true": "anger, threat to leave or dispute, legal words, safety, repeated contact, or a hard deadline",
            "false": "a routine question an automated answer can resolve",
        },
    },
    "sentiment": {
        "type": "choice",
        "instructions": "What is the customer's emotional state?",
        "criteria": {
            "angry": "hostile, insulting, threatening, or furious",
            "frustrated": "annoyed, disappointed, repeated problem, sarcastic",
            "neutral": "matter-of-fact question or statement",
            "positive": "thankful, happy, or complimentary",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How time-critical is this request?",
        "criteria": ["can wait days", "within a day", "within hours", "immediate"],
    },
    "intent": {
        "type": "choice",
        "instructions": "Which category best fits the request?",
        "criteria": {
            "order_status": "where is my order, tracking, late or missing delivery",
            "shipping_policy": "shipping costs, delivery times, options, Prime shipping",
            "returns_refunds": "return window, refund status, exchange, damaged item",
            "account_payment": "login, password, payment failed, charges, gift cards",
            "other": "anything else",
        },
    },
}

SENTIMENT_LABELS = list(ROUTER_QUESTIONS["sentiment"]["criteria"])
INTENT_LABELS = list(ROUTER_QUESTIONS["intent"]["criteria"])
URGENCY_CRITERIA: List[str] = list(ROUTER_QUESTIONS["urgency"]["criteria"])

# Default inference parameters
DEFAULT_ROUTER_TEMPERATURE = 1.18
MAX_STATE_CHARS = 1800  # Laya state length ceiling (~512 tokens)
CHUNK_OVERLAP_CHARS = 200
MAX_RERANK_CHOICES = 5  # Laya choice question maximum option ceiling


def clean_text(text: Optional[str]) -> str:
    """Sanitize customer inquiry text: normalize whitespace and strip excess newlines."""
    if not text:
        return ""
    # Replace carriage returns and excessive whitespace with a single space
    cleaned = re.sub(r"[\r\n\t]+", " ", str(text))
    cleaned = re.sub(r" +", " ", cleaned)
    return cleaned.strip()


def chunk_text(
    text: str,
    max_chars: int = MAX_STATE_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> List[str]:
    """Apply long-input policy: split text exceeding max_chars into overlapping chunks.

    Per TDD §2.1 and specs/router-s1.md:
    Inputs within max_chars are evaluated as a single chunk. Long inputs (e.g. pasted emails)
    are chunked with overlap so boundary context is preserved.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return [""]
    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks: List[str] = []
    start = 0
    text_len = len(cleaned)

    while start < text_len:
        end = min(start + max_chars, text_len)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start += max_chars - overlap_chars

    return chunks if chunks else [cleaned[:max_chars]]


def apply_temperature_scaling(
    probs: Dict[str, float],
    temperature: float = DEFAULT_ROUTER_TEMPERATURE,
) -> Dict[str, float]:
    """Apply temperature scaling to a probability distribution.

    Given probabilities p_i, calculates logits z_i = log(p_i + eps),
    applies temperature z_i / T, and renormalizes via softmax.
    A temperature T > 1.0 softens overconfident probabilities.
    """
    if not probs:
        return {}
    if abs(temperature - 1.0) < 1e-4:
        return {k: round(float(v), 4) for k, v in probs.items()}

    keys = list(probs.keys())
    eps = 1e-12
    # Convert probabilities to logits and scale by temperature
    scaled_logits = [math.log(max(float(probs[k]), eps)) / max(temperature, 1e-4) for k in keys]
    max_logit = max(scaled_logits)
    exp_logits = [math.exp(logit - max_logit) for logit in scaled_logits]
    sum_exp = sum(exp_logits)

    if sum_exp <= 0.0:
        uniform = 1.0 / len(keys)
        return {k: round(uniform, 4) for k in keys}

    return {k: round(exp_l / sum_exp, 4) for k, exp_l in zip(keys, exp_logits, strict=False)}


def extract_raw_probabilities(answer_obj: Any) -> Dict[str, float]:
    """Extract normalized probability dictionary from a raw Laya question answer."""
    if not isinstance(answer_obj, dict):
        return {}

    # Check for direct probabilities dict
    if "probabilities" in answer_obj and isinstance(answer_obj["probabilities"], dict):
        return {str(k): float(v) for k, v in answer_obj["probabilities"].items()}

    # Check choice_probabilities
    if "choice_probabilities" in answer_obj and isinstance(answer_obj["choice_probabilities"], dict):
        return {str(k): float(v) for k, v in answer_obj["choice_probabilities"].items()}

    # Check noul field (boolean question: float probability of true)
    if "noul" in answer_obj:
        try:
            p_true = float(answer_obj["noul"])
            p_true = max(0.0, min(1.0, p_true))
            return {"false": round(1.0 - p_true, 4), "true": round(p_true, 4)}
        except (ValueError, TypeError):
            pass

    return {}


def format_single_chunk_decision(
    raw_answers: Dict[str, Any],
    temperature: float = DEFAULT_ROUTER_TEMPERATURE,
) -> Dict[str, Any]:
    """Parse and temperature-scale router predictions for a single text chunk."""
    # 1. Escalate
    esc_ans = raw_answers.get("escalate", {})
    esc_probs = extract_raw_probabilities(esc_ans)
    if not esc_probs and "choice" in esc_ans:
        esc_probs = {"true": 1.0 if esc_ans["choice"] == "true" else 0.0, "false": 0.0}
    esc_scaled = apply_temperature_scaling(esc_probs, temperature)
    p_esc = esc_scaled.get("true", 0.0)

    # 2. Sentiment
    sent_ans = raw_answers.get("sentiment", {})
    sent_probs = extract_raw_probabilities(sent_ans)
    sent_scaled = apply_temperature_scaling(sent_probs, temperature)
    top_sent = max(sent_scaled, key=sent_scaled.get) if sent_scaled else "neutral"

    # 3. Urgency
    urg_ans = raw_answers.get("urgency", {})
    urg_probs = extract_raw_probabilities(urg_ans)
    urg_scaled = apply_temperature_scaling(urg_probs, temperature)
    top_urg_key = max(urg_scaled, key=urg_scaled.get) if urg_scaled else "0"
    try:
        urg_level = int(top_urg_key)
    except ValueError:
        urg_level = 0
    urg_label = URGENCY_CRITERIA[urg_level] if 0 <= urg_level < len(URGENCY_CRITERIA) else "normal"

    # 4. Intent
    intent_ans = raw_answers.get("intent", {})
    intent_probs = extract_raw_probabilities(intent_ans)
    intent_scaled = apply_temperature_scaling(intent_probs, temperature)
    top_intent = max(intent_scaled, key=intent_scaled.get) if intent_scaled else "other"

    return {
        "escalate": {
            "label": "escalate" if p_esc >= 0.50 else "do_not_escalate",
            "prob": p_esc,
            "probabilities": esc_scaled,
        },
        "sentiment": {
            "label": top_sent,
            "prob": sent_scaled.get(top_sent, 0.0),
            "probabilities": sent_scaled,
        },
        "urgency": {
            "level": urg_level,
            "label": urg_label,
            "prob": urg_scaled.get(top_urg_key, 0.0),
            "probabilities": urg_scaled,
        },
        "intent": {
            "label": top_intent,
            "prob": intent_scaled.get(top_intent, 0.0),
            "probabilities": intent_scaled,
        },
    }


def aggregate_chunk_decisions(
    chunk_decisions: Sequence[Dict[str, Any]],
    model_version: str = "v2",
    latency_ms: float = 0.0,
) -> Dict[str, Any]:
    """Aggregate decisions across multiple chunks using the TDD long-input policy.

    Aggregation rules per TDD §2.1 and §3.6:
    - Escalate: max pooling over P(escalate) across all chunks (if any chunk indicates escalation, escalate).
    - Urgency: max pooling over urgency level (highest urgency chunk wins).
    - Sentiment: mean pooling over class probabilities across chunks.
    - Intent: mean pooling over class probabilities across chunks.
    """
    if not chunk_decisions:
        return {
            "escalate": {"label": "do_not_escalate", "prob": 0.0, "probabilities": {"false": 1.0, "true": 0.0}},
            "sentiment": {"label": "neutral", "prob": 1.0, "probabilities": {"neutral": 1.0}},
            "urgency": {"level": 0, "label": "can wait days", "prob": 1.0, "probabilities": {"0": 1.0}},
            "intent": {"label": "other", "prob": 1.0, "probabilities": {"other": 1.0}},
            "model_version": model_version,
            "latency_ms": round(latency_ms, 2),
        }

    if len(chunk_decisions) == 1:
        res = copy_dict(chunk_decisions[0])
        res["model_version"] = model_version
        res["latency_ms"] = round(latency_ms, 2)
        return res

    n = len(chunk_decisions)

    # 1. Escalate: max pooling on P(true)
    max_p_esc = max(c["escalate"]["prob"] for c in chunk_decisions)
    escalate_label = "escalate" if max_p_esc >= 0.50 else "do_not_escalate"
    agg_esc = {
        "label": escalate_label,
        "prob": round(max_p_esc, 4),
        "probabilities": {"false": round(1.0 - max_p_esc, 4), "true": round(max_p_esc, 4)},
    }

    # 2. Urgency: max pooling on level
    max_urg_dec = max(chunk_decisions, key=lambda c: (c["urgency"]["level"], c["urgency"]["prob"]))
    agg_urg = copy_dict(max_urg_dec["urgency"])

    # 3. Sentiment: mean pooling on probabilities
    all_sent_keys = set().union(*(c["sentiment"]["probabilities"].keys() for c in chunk_decisions))
    mean_sent_probs = {
        k: sum(c["sentiment"]["probabilities"].get(k, 0.0) for c in chunk_decisions) / n
        for k in all_sent_keys
    }
    norm_sent_sum = sum(mean_sent_probs.values()) or 1.0
    mean_sent_probs = {k: round(v / norm_sent_sum, 4) for k, v in mean_sent_probs.items()}
    top_sent = max(mean_sent_probs, key=mean_sent_probs.get) if mean_sent_probs else "neutral"
    agg_sent = {
        "label": top_sent,
        "prob": mean_sent_probs.get(top_sent, 0.0),
        "probabilities": mean_sent_probs,
    }

    # 4. Intent: mean pooling on probabilities
    all_intent_keys = set().union(*(c["intent"]["probabilities"].keys() for c in chunk_decisions))
    mean_intent_probs = {
        k: sum(c["intent"]["probabilities"].get(k, 0.0) for c in chunk_decisions) / n
        for k in all_intent_keys
    }
    norm_intent_sum = sum(mean_intent_probs.values()) or 1.0
    mean_intent_probs = {k: round(v / norm_intent_sum, 4) for k, v in mean_intent_probs.items()}
    top_intent = max(mean_intent_probs, key=mean_intent_probs.get) if mean_intent_probs else "other"
    agg_intent = {
        "label": top_intent,
        "prob": mean_intent_probs.get(top_intent, 0.0),
        "probabilities": mean_intent_probs,
    }

    return {
        "escalate": agg_esc,
        "sentiment": agg_sent,
        "urgency": agg_urg,
        "intent": agg_intent,
        "model_version": model_version,
        "latency_ms": round(latency_ms, 2),
    }


def copy_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Deep copy helper for simple JSON-serializable dictionaries."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = copy_dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def build_rerank_choice_question(
    candidate_ids: Sequence[str],
    candidate_texts: Dict[str, str],
) -> Dict[str, Any]:
    """Construct a Laya choice question dictionary for candidate document reranking."""
    criteria: Dict[str, str] = {}
    for cid in candidate_ids:
        text = candidate_texts.get(cid, cid)
        # Enforce maximum criteria text length (~300 chars per candidate option)
        criteria[cid] = clean_text(text)[:300]

    return {
        "type": "choice",
        "instructions": "Select the most relevant FAQ article to answer the customer's question.",
        "criteria": criteria,
    }


def format_rerank_response(
    candidates: Sequence[Dict[str, str]],
    choice_probs: Dict[str, float],
    latency_ms: float = 0.0,
) -> List[Dict[str, Any]]:
    """Format and sort candidate documents by predicted choice probability descending.

    Top candidates (up to 5) are reordered by their predicted probability. Candidates
    past rank 5 (if provided) preserve their initial relative ordering with calibrated
    tail scores below the lowest top-5 score. Each candidate dictionary includes
    per-instance server `latency_ms` for schema parity with the router output.
    """
    lat = round(latency_ms, 2)
    if not candidates:
        return []

    top_head = candidates[:MAX_RERANK_CHOICES]
    tail = candidates[MAX_RERANK_CHOICES:]

    scored_head: List[Dict[str, Any]] = []
    for idx, c in enumerate(top_head):
        cid = str(c.get("id", f"doc_{idx}"))
        prob = float(choice_probs.get(cid, 0.0))
        scored_head.append({
            "id": cid,
            "score": round(prob, 6),
            "original_rank": idx,
        })

    # Sort head by probability descending, breaking ties by original rank
    scored_head.sort(key=lambda item: (item["score"], -item["original_rank"]), reverse=True)

    min_head_score = min((item["score"] for item in scored_head), default=0.01)
    base_tail_score = max(0.0, min_head_score * 0.5)

    scored_tail: List[Dict[str, Any]] = []
    for offset, c in enumerate(tail):
        cid = str(c.get("id", f"doc_{MAX_RERANK_CHOICES + offset}"))
        # Calibrate tail score to decrease monotonically with rank
        score = base_tail_score / (1.0 + (offset + 1) * 0.1)
        scored_tail.append({
            "id": cid,
            "score": round(score, 6),
        })

    # Clean out internal sorting helper keys and include latency_ms for schema parity
    result: List[Dict[str, Any]] = []
    for item in scored_head:
        result.append({"id": item["id"], "score": item["score"], "latency_ms": lat})
    for item in scored_tail:
        result.append({"id": item["id"], "score": item["score"], "latency_ms": lat})

    return result
