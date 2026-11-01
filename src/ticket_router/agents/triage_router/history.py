"""Pure detector functions and rule evaluation for conversation history requests.

# REQ-HIST-001: Deterministic Conversation History Access Detection and Turn Ordering
# REQ-HIST-002: Server-Side Authentication & Cross-Customer History Isolation
"""

from __future__ import annotations

import re
from typing import Optional

# Memory Bank read budget when the customer explicitly asks for their history (REQ-HIST-002);
# larger than the 300 ms per-turn budget used for background context.
HISTORY_MEMORY_TIMEOUT_S = 2.5

# Patterns indicating complaint, emergency, legal, or explicit human requests that bypass self-service
_HUMAN_REQUEST_REGEX = re.compile(
    r"\b(speak to a human|talk to a human|speak with human|real person|human agent|operator|representative|manager|agent)\b",
    re.IGNORECASE,
)

_EMERGENCY_REGEX = re.compile(
    r"\b(suicide|self-harm|kill myself|end my life|weapon|bomb|police|lawyer|attorney|lawsuit|subpoena|court|sue you)\b",
    re.IGNORECASE,
)

_ABUSE_REGEX = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard\w*|asshole\w*|cunt\w*|dick\w*|idiot\w*|moron\w*|scam\w*|burn in hell|go die\w*|kill yourself)\b",
    re.IGNORECASE,
)

_HARD_ESCALATE_REGEX = re.compile(
    r"\b(fraud|fraudulent|chargeback|dispute\s+charge|lawyer|attorney|lawsuit|subpoena|court|sue\b|stolen\s+card|unauthorized\s+charge)\b",
    re.IGNORECASE,
)


def matches_conversation_history_escalation_exception(text: str) -> bool:
    """Return True if message requests a human or contains Tier 0 hard-escalate keywords.

    When True, normal routing policy executes (Tier 0 emergency/abuse/human or Tier 1/2 models).
    """
    if not text or not text.strip():
        return False
    if _HUMAN_REQUEST_REGEX.search(text):
        return True
    if _EMERGENCY_REGEX.search(text) or _ABUSE_REGEX.search(text):
        return True
    if _HARD_ESCALATE_REGEX.search(text):
        return True
    return False


def detect_conversation_history_request(text: str) -> bool:
    """Detect whether inbound text is a request for conversation or chat history.

    Pure function adhering to REQ-HIST-001.

    Recognises:
    - Previous conversations, chat history, past interactions, earlier chats
    - Conversation summaries ("what did we talk about last time", "summarize my previous chats")
    - Access requests ("access to all the previous conversations", "show my chat history")

    Must NOT fire on:
    - Order history ("show my previous orders", "order history")
    - Past tickets status by TICK id (ticket lifecycle)
    - Generic history of a product, store, or company ("history of Google")
    """
    if not text or not text.strip():
        return False

    t = text.strip().lower()

    # Negative boundary 1: Explicit TICK reference (that is ticket lifecycle or status inquiry)
    if re.search(r"\b(tick-[a-z0-9_-]+)\b", t, re.IGNORECASE):
        return False

    # Negative boundary 2: Order / purchase / shipment / product / package history
    if re.search(
        r"\b(?:order|orders|purchase|purchases|bought|package|packages|shipment|shipments|delivery|deliveries|tracking|charge|charges|transaction|transactions|item|items)\s+history\b",
        t,
    ):
        return False
    if re.search(
        r"\bhistory\s+of\s+(?:my\s+|our\s+)?(?:order|orders|purchase|purchases|package|packages|shipment|shipments|delivery|deliveries|items?)\b",
        t,
    ):
        return False
    if re.search(
        r"\b(?:previous|past|earlier|prior|recent)\s+(?:orders|purchases|packages|shipments|deliveries|items|charges|transactions)\b",
        t,
    ) and not re.search(r"\b(?:conversations?|chats?|interactions?|transcripts?|messages?)\b", t):
        return False

    # Negative boundary 3: Generic company, brand, store, app, or product history
    if re.search(
        r"\bhistory\s+of\s+(?:the\s+|this\s+|your\s+)?(?:company|brand|store|shop|business|product|service|google|app|organization|site|website|firm)\b",
        t,
    ):
        return False
    if re.search(
        r"\b(?:company|brand|store|shop|business|product|service)\s+history\b",
        t,
    ):
        return False

    # Positive boundary 1: Direct conversation nouns combined with temporal/history modifiers
    conv_nouns = r"(?:conversations?|chats?|interactions?|transcripts?|messages?|discussions?)"

    # "previous conversations", "earlier chats", "past interactions", "prior chats"
    if re.search(rf"\b(?:previous|earlier|past|prior|prior-to-this|last|our\s+past)\s+{conv_nouns}\b", t):
        return True

    # "chat history", "conversation history", "message history"
    if re.search(r"\b(?:conversation|chat|interaction|message|discussion)\s+history\b", t):
        return True

    # "history of my conversations", "history of our chats"
    if re.search(rf"\bhistory\s+of\s+(?:all\s+)?(?:the\s+|my\s+|our\s+)?{conv_nouns}\b", t):
        return True

    # "past interactions"
    if re.search(r"\bpast\s+interactions\b", t):
        return True

    # Positive boundary 2: Action verbs requesting access / view / summary of conversation history
    if re.search(
        rf"\b(?:access\s+(?:to\s+)?|view\s+|show\s+(?:me\s+)?|see\s+|get\s+|pull\s+up\s+|fetch\s+|summarize\s+|summary\s+of\s+|review\s+|look\s+at\s+)"
        rf"(?:all\s+(?:the\s+|my\s+)?)?(?:previous|earlier|past|prior|my|our)?\s*{conv_nouns}\b",
        t,
    ):
        return True

    # Positive boundary 3: "what did we talk about last time", "what did we discuss earlier", etc.
    if (
        re.search(
            r"\bwhat\s+(?:did\s+we|have\s+we)\s+(?:talk|speak|discuss|chat)\s+(?:about|regarding)?\s*(?:last\s+time|earlier|previously|before)?\b",
            t,
        )
        and any(k in t for k in ("talk", "speak", "discuss", "chat"))
        and any(k in t for k in ("last time", "earlier", "previously", "before", "last", "prior"))
    ):
        return True

    if re.search(r"\b(?:what\s+were\s+we\s+talking\s+about|what\s+was\s+our\s+last\s+conversation\s+about)\b", t):
        return True

    if re.search(r"\bwhat\s+did\s+i\s+(?:ask|say|mention)\s+(?:last\s+time|earlier|previously|before)\b", t):
        return True

    # "summarize my previous chats", "summarize our last conversation"
    if re.search(
        rf"\bsummariz\w*\s+(?:all\s+)?(?:the\s+|my\s+|our\s+)?(?:previous|earlier|past|prior|last)?\s*{conv_nouns}\b",
        t,
    ):
        return True

    # Positive boundary 4: Cross-customer or multi-user conversation history requests
    if is_cross_customer_history_request(text) and re.search(rf"\b{conv_nouns}\b", t):
        return True

    return False


def is_cross_customer_history_request(
    text: str,
    verified_customer_id: Optional[str] = None,
) -> bool:
    """Detect requests for another customer's or all users' conversations (REQ-HIST-002)."""
    if not text or not text.strip():
        return False

    t = text.strip().lower()
    # Strip social handle placeholders like @user
    t = re.sub(r"@user\b", "", t)

    conv_nouns = r"(?:conversations?|chats?|interactions?|transcripts?|messages?|history|logs?)"

    # 1. Phrases asking for everyone's, all users', or other customers' conversations
    cross_phrases = [
        r"\b(?:everyone(?:'s)?|all\s+(?:other\s+)?users?(?:'s)?|all\s+(?:other\s+)?customers?(?:'s)?)\s+(?:conversations?|chats?|messages?|history)\b",
        r"\b(?:conversations?|chats?|messages?|history)\s+(?:of|for|from)\s+(?:everyone|all\s+users?|all\s+customers?)\b",
        r"\bshow\s+(?:me\s+)?everyone(?:'s)?\s+chats?\b",
        r"\bshow\s+(?:me\s+)?all\s+(?:users|customers)(?:'s)?\s+chats?\b",
        r"\b(?:access\s+to\s+)?(?:all\s+users?|all\s+customers?|everyone(?:'s)?)\s+(?:conversations?|chats?)\b",
        r"\bother\s+(?:users?|customers?)(?:'s)?\s+(?:conversations?|chats?|history)\b",
        r"\b(?:conversations?|chats?)\s+of\s+other\s+(?:users?|customers?)\b",
        r"\bsomeone\s+else(?:'s)?\s+(?:conversations?|chats?)\b",
        r"\ball\s+chat\s+logs\s+for\s+all\s+users\b",
    ]
    if any(re.search(p, t) for p in cross_phrases):
        return True

    # 2. Specific customer ID referenced in request that differs from verified caller
    has_history_intent = bool(
        re.search(
            r"\b(?:access\s+(?:to\s+)?|view\s+|show\s+(?:me\s+)?|see\s+|get\s+|pull\s+up\s+|fetch\s+|summarize\s+|summary\s+of\s+|review\s+|look\s+at\s+|(?:conversations?|chats?|messages?|history)\s+(?:of|for|from))\b",
            t,
        )
    )

    if has_history_intent and re.search(rf"\b{conv_nouns}\b", t):
        cid_match = re.search(r"\b(?:customer|user|account)?\s*(cust_\d+|cust-[a-z0-9_-]+)\b", t)
        if cid_match:
            requested_cid = cid_match.group(1).lower()
            if (verified_customer_id and requested_cid != verified_customer_id.lower()) or (not verified_customer_id):
                return True

        cust_num_match = re.search(r"\b(?:customer|user)\s+id\s*[:=]?\s*([a-z0-9_-]+)\b", t)
        if cust_num_match:
            candidate = cust_num_match.group(1).lower()
            if candidate not in (
                "service", "services", "support", "care", "agent", "history", "previous", "earlier", "chat", "chats"
            ) and ((verified_customer_id and candidate != verified_customer_id.lower()) or (not verified_customer_id)):
                return True

    return False
