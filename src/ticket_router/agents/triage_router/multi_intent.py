"""Multi-intent question deconstruction and sub-question detection.

# REQ-SAFE-005: Multi-Intent Sentence Deconstruction and Dual-Pipeline Grounded Resolution
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

_REFUND_KEYWORDS = re.compile(
    r"\b(?:refund|refunds|return|returns|exchange|exchanges|money\s+back|warranty)\b",
    re.IGNORECASE,
)
_SHIPPING_KEYWORDS = re.compile(
    r"\b(?:shipping\s+policy|shipping\s+speed|shipping\s+options?|shipping\s+fee|delivery\s+time|delivery\s+options?|apo|fpo)\b",
    re.IGNORECASE,
)
_ORDER_KEYWORDS = re.compile(
    r"\b(?:order|orders|package|packages|parcel|tracking|late|delayed|delay|where\s+is|status\s+of|delivery\s+status|shipment)\b",
    re.IGNORECASE,
)
_ACCOUNT_KEYWORDS = re.compile(
    r"\b(?:account|login|password|sign\s+in|payment\s+method|credit\s+card|billing\s+policy|gift\s+card)\b",
    re.IGNORECASE,
)


def classify_sub_intent(text: str) -> Optional[str]:
    """Classify the primary intent of a single sub-clause/question."""
    if not text or not text.strip():
        return None
    t = text.strip()

    # Prioritize order intent if asking about order status / late package without refund keywords
    if _ORDER_KEYWORDS.search(t) and not _REFUND_KEYWORDS.search(t) and not _SHIPPING_KEYWORDS.search(t):
        return "order_status"

    if _REFUND_KEYWORDS.search(t):
        return "returns_refunds"

    if _SHIPPING_KEYWORDS.search(t):
        return "shipping_policy"

    if _ACCOUNT_KEYWORDS.search(t):
        return "account_payment"

    if _ORDER_KEYWORDS.search(t):
        return "order_status"

    return None


def detect_sub_questions(text: str) -> List[Dict[str, str]]:
    """Deconstruct multi-intent message into distinct sub-questions (REQ-SAFE-005).

    e.g. 'my order is late. also, what is the refund policy?' ->
    [
        {'text': 'my order is late', 'intent': 'order_status'},
        {'text': 'what is the refund policy', 'intent': 'returns_refunds'},
    ]
    """
    if not text or not text.strip():
        return []

    t = text.strip()

    split_pattern = re.compile(
        r"(?:[.!?]+\s+|\n+|(?:,\s*)?(?:\balso\b|\badditionally\b|\bplus\b|\band\s+also\b)[,:]?\s*|;\s*|\band\s+(?:what|how|where|when|can|could|is|are|do|does)\b\s*)",
        re.IGNORECASE,
    )

    parts = [p.strip() for p in split_pattern.split(t) if p and len(p.strip()) >= 5]
    if len(parts) <= 1:
        return []

    sub_questions: List[Dict[str, str]] = []
    seen_intents: set[str] = set()

    for part in parts:
        intent = classify_sub_intent(part)
        if intent and intent not in seen_intents:
            seen_intents.add(intent)
            sub_questions.append({"text": part, "intent": intent})

    if len(sub_questions) >= 2:
        return sub_questions

    return []
