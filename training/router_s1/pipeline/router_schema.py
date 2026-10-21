"""router_schema.py — the typed questions the Tier-1 router (Laya) answers.

Single source of truth used by labeling, dataset building, training and evaluation.
Constraints from the Laya protocol: <=5 options per choice question, keep option order stable
at inference (call Vertex with :rawPredict), state <= ~1,800 chars after cleaning.
"""

QUESTIONS = {
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

SENTIMENTS = list(QUESTIONS["sentiment"]["criteria"])
INTENTS = list(QUESTIONS["intent"]["criteria"])
URGENCY_LEVELS = len(QUESTIONS["urgency"]["criteria"])
