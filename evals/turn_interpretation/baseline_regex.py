"""Frozen baseline regex implementation of triage routing and natural-language interpretation.

Copied from commit a48a288 for evaluation and comparison against the new LLM-based turn interpreter.
DO NOT IMPORT THIS MODULE FROM src/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Tier 0 Deterministic Rules (from policy.py @ a48a288)
# -----------------------------------------------------------------------------

# Emergency & Legal triggers
_EMERGENCY_REGEX = re.compile(
    r"\b(suicide|self-harm|kill myself|end my life|weapon|bomb|police|lawyer|attorney|lawsuit|subpoena|court|sue you)\b",
    re.IGNORECASE,
)

# Abuse / Toxicity / Profanity triggers
_ABUSE_REGEX = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard\w*|asshole\w*|cunt\w*|dick\w*|idiot\w*|moron\w*|scam\w*|burn in hell|go die\w*|kill yourself)\b",
    re.IGNORECASE,
)

# Explicit human representative request
_HUMAN_REQUEST_REGEX = re.compile(
    r"\b(speak to a human|talk to a human|speak with human|real person|human agent|operator|representative|manager)\b",
    re.IGNORECASE,
)

# Standalone conversational greetings
_GREETING_REGEX = re.compile(
    r"^(?:"
    r"(?:hi|hey|hello|howdy|hiya|greetings|good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|team|support|assistant|folks|everyone|all))?"
    r"(?:[\s,!?.]+(?:how\s+are\s+you(?:\s+doing|\s+today)?|how['’]?s\s+it\s+going|how\s+can\s+you\s+help(?:\s+me)?|what\s+can\s+you\s+(?:do|help(?:\s+me)?\s+with)))?"
    r"|"
    r"(?:how\s+can\s+you\s+help(?:\s+me)?|what\s+can\s+you\s+(?:do|help(?:\s+me)?\s+with))"
    r")[\s!?.]*$",
    re.IGNORECASE,
)

# Standalone conversational pleasantries
_PLEASANTRY_REGEX = re.compile(
    r"^(?:"
    r"(?:(?:ok|okay)[\s,]+)?(?:thanks|thank\s+you|thx|ty|many\s+thanks|much\s+appreciated)"
    r"(?:\s+(?:so\s+much|very\s+much|a\s+lot|a\s+bunch))?"
    r"(?:\s+for\s+(?:your|the)\s+(?:help|assistance|support|info|information))?"
    r"(?:\s+(?:team|support|assistant))?"
    r"|"
    r"(?:(?:thanks|thank\s+you)[\s,]+)?have\s+a\s+(?:great|good|nice|wonderful|lovely)\s+(?:day|evening|weekend)"
    r"|"
    r"(?:i\s+)?appreciate\s+(?:it|that|your\s+help|the\s+help)"
    r"|"
    r"cheers"
    r"|"
    r"much\s+obliged"
    r"|"
    r"that(?:'s|’s|\s+is)\s+(?:helpful|great|perfect|awesome)(?:[\s,]+(?:thanks|thank\s+you|thx|ty))"
    r"|"
    r"bye|goodbye|good\s+bye|bye\s+bye|see\s+you"
    r")[\s!?.]*$",
    re.IGNORECASE,
)

_HARD_ESCALATE_REGEX = re.compile(
    r"\b(fraud|fraudulent|chargeback|dispute\s+charge|lawyer|attorney|lawsuit|subpoena|court|sue\b|stolen\s+card|unauthorized\s+charge)\b",
    re.IGNORECASE,
)

_ORDER_LOOKUP_EXCLUDE_REGEX = re.compile(
    r"\b("
    r"refund\w*|"
    r"return\w*|"
    r"cancel\w*|"
    r"damaged?|"
    r"broken|"
    r"wrong|"
    r"missing|"
    r"late|"
    r"never\s+arrived|"
    r"angry|"
    r"ridiculous|"
    r"scam\w*|"
    r"lawyer\w*|"
    r"attorney\w*|"
    r"help\s+me|"
    r"urgent\w*|"
    r"asap"
    r")\b|!{2,}",
    re.IGNORECASE,
)

_ORDER_LOOKUP_REGEX = re.compile(
    r"^(?!.*(?:\b(?:refund\w*|return\w*|cancel\w*|damaged?|broken|wrong|missing|late|never\s+arrived|angry|ridiculous|scam\w*|lawyer\w*|attorney\w*|help\s+me|urgent\w*|asap)\b|!{2,}))(?:"
    r"what(?:\s+are|'re)\s+my\s+(?:recent\s+)?orders(?:\s+recently)?"
    r"|"
    r"what\s+(?:did|have)\s+i\s+(?:buy|bought|order|ordered|purchase|purchased)(?:\s+(?:recently|lately))?"
    r"|"
    r"(?:(?:can|could)\s+you\s+)?(?:show|list|give\s+me|give|get|check|see|view)(?:\s+me)?\s+my\s+(?:recent\s+|latest\s+|past\s+)?(?:orders|purchases|order\s+history)(?:\s+(?:from|in)\s+the\s+(?:last|past)\s+(?:week|month|\d+\s+days))?"
    r"|"
    r"my\s+(?:recent\s+|latest\s+|past\s+)?(?:orders|purchases|order\s+history)"
    r"|"
    r"(?:check|see|view)\s+my\s+(?:recent\s+)?orders"
    r"|"
    r"order\s+history"
    r"|"
    r"(?:where\s+is|where['’]?s)\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:what['’]?s|what\s+is)\s+the\s+)?status\s+of\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:can|could)\s+you\s+)?(?:track|tracking|locate|find|check)(?:\s+(?:the\s+)?status\s+of)?\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r"|"
    r"(?:(?:my|the)\s+order\s+)?ORD-\d{3,}\s+status"
    r"|"
    r"status\s+(?:(?:my|the)\s+order\s+)?ORD-\d{3,}"
    r")[\s!?.]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Tier0Result:
    matched: bool = False
    escalate: bool = False
    reason: Optional[str] = None
    category: Optional[str] = None
    sentiment: str = "neutral"
    urgency: str = "normal"


# -----------------------------------------------------------------------------
# Security Intents (from security_intents.py @ a48a288)
# -----------------------------------------------------------------------------

_ORDER_NOUNS = r"(?:orders?|purchases?|packages?|shipments?|deliveries?|items?|tracking|receipts?)"
_CONV_NOUNS = r"(?:conversations?|chats?|interactions?|transcripts?|messages?|history|logs?|discussions?)"
_ACCOUNT_NOUNS = r"(?:accounts?|profiles?|addresses?|tickets?|cards?|credentials?|details?|information|data)"
_ALL_TARGET_NOUNS = rf"(?:{_ORDER_NOUNS}|{_CONV_NOUNS}|{_ACCOUNT_NOUNS})"

_CROSS_CUSTOMER_PHRASES = [
    rf"\b(?:{_ALL_TARGET_NOUNS})\s+(?:of|for|from|belonging\s+to)\s+(?:other\s+|another\s+|all\s+(?:other\s+)?)(?:customers?|users?|people|accounts?|persons?)\b",
    rf"\b(?:other|another|everyone(?:'s)?|all)\s+(?:customers?|users?|people|accounts?)(?:'s)?\s+{_ALL_TARGET_NOUNS}\b",
    rf"\b(?:show|give|fetch|get|list|display|find|provide|see|view|pull\s+up|access)\s+(?:me\s+)?(?:all\s+)?(?:other\s+|another\s+|someone\s+else(?:'s)?|everyone(?:'s)?|all\s+customers?(?:'s)?)\s+{_ALL_TARGET_NOUNS}\b",
    rf"\b(?:show|give|fetch|get|list|display|find|provide|see|view|pull\s+up|access)\s+(?:me\s+)?{_ALL_TARGET_NOUNS}\s+(?:of|for|from)\s+(?:other\s+|another\s+|someone\s+else|everyone|all)\s*(?:customers?|users?|people|accounts?)?\b",
    r"\ball\s+(?:chat\s+logs|support\s+tickets|customer\s+accounts|customer\s+orders)\b",
    r"\bgive\s+me\s+orders\s+of\s+other\s+customers\b",
    r"\borders\s+of\s+other\s+customers\b",
]
_CROSS_REGEXES = [re.compile(p, re.IGNORECASE) for p in _CROSS_CUSTOMER_PHRASES]

_AUTH_BYPASS_PATTERNS = [
    r"\b(?:skip|bypass|disable|override|ignore|circumvent|turn\s+off)\s+(?:the\s+)?(?:authorization|auth|verification|security|identity|authentication)\s*(?:checks?|flow|rules?|layer)?\b",
    r"\b(?:you\s+can\s+)?(?:skip|bypass|disable)\s+(?:the\s+)?(?:authorization|auth|verification|security|identity)\s*checks?\b",
    r"\bauthorization\s+bypass\b",
    r"\bbypass\s+(?:the\s+)?(?:auth|authorization|verification|authentication|security\s+check)\b",
    r"\b(?:i\s+am|i'm|im|acting\s+as)\s+(?:an?\s+)?(?:administrator|admin|system\s+operator|super\s*user|root)\b",
]
_AUTH_BYPASS_REGEXES = [re.compile(p, re.IGNORECASE) for p in _AUTH_BYPASS_PATTERNS]


def is_cross_customer_data_request(text: str, verified_customer_id: Optional[str] = None) -> bool:
    if not text or not text.strip():
        return False
    t = text.strip().lower()
    t = re.sub(r"@user\b", "", t)
    if any(r.search(t) for r in _CROSS_REGEXES):
        return True
    has_access_verb = bool(
        re.search(
            rf"\b(?:access\s+(?:to\s+)?|view\s+|show\s+(?:me\s+)?|see\s+|get\s+|give\s+(?:me\s+)?|pull\s+up\s+|fetch\s+|summarize\s+|summary\s+of\s+|review\s+|look\s+at\s+|{_ALL_TARGET_NOUNS}\s+(?:of|for|from))\b",
            t,
        )
    )
    if has_access_verb:
        cid_match = re.search(r"\b(?:customer|user|account)?\s*(cust_\d+|cust-[a-z0-9_-]+)\b", t)
        if cid_match:
            candidate = cid_match.group(1).lower()
            if (verified_customer_id and candidate != verified_customer_id.lower()) or (not verified_customer_id):
                return True
        cust_num_match = re.search(r"\b(?:customer|user)\s+id\s*[:=]?\s*([a-z0-9_-]+)\b", t)
        if cust_num_match:
            candidate = cust_num_match.group(1).lower()
            if candidate not in (
                "service", "services", "support", "care", "agent", "history", "previous", "earlier", "chat", "chats", "order", "orders"
            ) and ((verified_customer_id and candidate != verified_customer_id.lower()) or (not verified_customer_id)):
                return True
    return False


def is_authorization_bypass_attempt(text: str) -> bool:
    if not text or not text.strip():
        return False
    t = text.strip().lower()
    return any(r.search(t) for r in _AUTH_BYPASS_REGEXES)


def detect_security_intent(text: str, verified_customer_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
    if not text or not text.strip():
        return None
    if is_authorization_bypass_attempt(text):
        return ("authorization_bypass_attempt", "auth_bypass_refusal")
    if is_cross_customer_data_request(text, verified_customer_id=verified_customer_id):
        return ("cross_customer_data_request", "cross_customer_refusal")
    return None


# -----------------------------------------------------------------------------
# Conversation History (from history.py @ a48a288)
# -----------------------------------------------------------------------------

def detect_conversation_history_request(text: str) -> bool:
    if not text or not text.strip():
        return False
    t = text.strip().lower()
    if re.search(r"\b(tick-[a-z0-9_-]+)\b", t, re.IGNORECASE):
        return False
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
    if re.search(
        r"\bhistory\s+of\s+(?:the\s+|this\s+|your\s+)?(?:company|brand|store|shop|business|product|service|google|app|organization|site|website|firm)\b",
        t,
    ):
        return False
    if re.search(r"\b(?:company|brand|store|shop|business|product|service)\s+history\b", t):
        return False

    conv_nouns = r"(?:conversations?|chats?|interactions?|transcripts?|messages?|discussions?)"
    if re.search(rf"\b(?:previous|earlier|past|prior|prior-to-this|last|our\s+past)\s+{conv_nouns}\b", t):
        return True
    if re.search(r"\b(?:conversation|chat|interaction|message|discussion)\s+history\b", t):
        return True
    if re.search(rf"\bhistory\s+of\s+(?:all\s+)?(?:the\s+|my\s+|our\s+)?{conv_nouns}\b", t):
        return True
    if re.search(r"\bpast\s+interactions\b", t):
        return True
    if re.search(
        rf"\b(?:access\s+(?:to\s+)?|view\s+|show\s+(?:me\s+)?|see\s+|get\s+|pull\s+up\s+|fetch\s+|summarize\s+|summary\s+of\s+|review\s+|look\s+at\s+)"
        rf"(?:all\s+(?:the\s+|my\s+)?)?(?:previous|earlier|past|prior|my|our)?\s*{conv_nouns}\b",
        t,
    ):
        return True
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
    if re.search(
        rf"\bsummariz\w*\s+(?:all\s+)?(?:the\s+|my\s+|our\s+)?(?:previous|earlier|past|prior|last)?\s*{conv_nouns}\b",
        t,
    ):
        return True
    if is_cross_customer_data_request(text) and re.search(rf"\b{conv_nouns}\b", t):
        return True
    return False


def matches_conversation_history_escalation_exception(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _HUMAN_REQUEST_REGEX.search(text):
        return True
    if _EMERGENCY_REGEX.search(text) or _ABUSE_REGEX.search(text):
        return True
    if _HARD_ESCALATE_REGEX.search(text):
        return True
    return False


def evaluate_tier0_rules(text: str) -> Tier0Result:
    if not text or not text.strip():
        return Tier0Result(matched=False)

    sec_intent = detect_security_intent(text)
    if sec_intent:
        intent_name, _ = sec_intent
        return Tier0Result(
            matched=True,
            escalate=False,
            reason=intent_name,
            category=intent_name,
            sentiment="neutral",
            urgency="normal",
        )

    match_em = _EMERGENCY_REGEX.search(text)
    if match_em:
        term = match_em.group(0).lower()
        cat = "legal" if term in ("lawyer", "attorney", "lawsuit", "subpoena", "court", "sue you") else "emergency"
        return Tier0Result(
            matched=True,
            escalate=True,
            reason=f"tier0_{cat}_trigger",
            category=cat,
            sentiment="angry" if cat == "legal" else "neutral",
            urgency="immediate",
        )

    if _ABUSE_REGEX.search(text):
        return Tier0Result(
            matched=True,
            escalate=True,
            reason="tier0_abusive_language",
            category="abuse",
            sentiment="angry",
            urgency="normal",
        )

    if _HUMAN_REQUEST_REGEX.search(text):
        return Tier0Result(
            matched=True,
            escalate=True,
            reason="tier0_human_agent_request",
            category="human_request",
            sentiment="neutral",
            urgency="normal",
        )

    cleaned = text.strip()
    if _GREETING_REGEX.match(cleaned):
        return Tier0Result(
            matched=True,
            escalate=False,
            reason="tier0_conversational_greeting",
            category="greeting",
            sentiment="neutral",
            urgency="normal",
        )

    if _PLEASANTRY_REGEX.match(cleaned):
        return Tier0Result(
            matched=True,
            escalate=False,
            reason="tier0_conversational_pleasantry",
            category="pleasantry",
            sentiment="positive",
            urgency="normal",
        )

    if detect_conversation_history_request(text) and not matches_conversation_history_escalation_exception(text):
        return Tier0Result(
            matched=True,
            escalate=False,
            reason="conversation_history_access",
            category="conversation_history",
            sentiment="neutral",
            urgency="normal",
        )

    return Tier0Result(matched=False)


def matches_order_lookup(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if _ORDER_LOOKUP_EXCLUDE_REGEX.search(cleaned):
        return False
    return bool(_ORDER_LOOKUP_REGEX.match(cleaned))


# -----------------------------------------------------------------------------
# Agent Lifecycle & Follow-up Regexes (from agent.py @ a48a288)
# -----------------------------------------------------------------------------

def extract_ticket_id_reference(text: str) -> Optional[str]:
    match = re.search(r"\b(TICK-[A-Za-z0-9_-]+)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def is_explicit_ticket_reference(text: str) -> bool:
    if extract_ticket_id_reference(text):
        return True
    t = text.lower()
    return any(
        p in t
        for p in (
            "my ticket",
            "open ticket",
            "my open ticket",
            "the specialist",
            "my specialist",
            "ticket status",
            "status of my ticket",
        )
    )


def is_conditional_resolution(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if re.search(r"\bif\b.*\b(?:good|fine|ok|okay|all\s+set|resolved|helped)\b", t):
        return True
    if re.search(r"\b(?:hopefully|assuming|provided)\b.*\b(?:arrives?|comes?|delivers?)\b", t):
        return True
    return False


def is_unconditional_resolved(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if is_conditional_resolution(t):
        return False
    patterns = [
        r"\b(?:it|package|order|item)\s+(?:arrived|was\s+delivered|came\s+in|is\s+here)\b",
        r"\b(?:i\s+received\s+it|received\s+my\s+package|received\s+my\s+order)\b",
        r"\b(?:everything\s+is\s+resolved|my\s+issue\s+is\s+resolved|problem\s+is\s+fixed)\b",
    ]
    return any(re.search(p, t) for p in patterns)


def is_affirmative_resolution(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if is_conditional_resolution(t):
        return False
    if re.search(
        r"\b(but|however|still|not|didn't|does not|unresolved|haven't|problem|issue|broken|help|urgent|agent|human|here)\b",
        t,
    ):
        return False
    patterns = [
        r"^yes[!.?]*$",
        r"^yep[!.?]*$",
        r"^yeah[!.?]*$",
        r"^yes\s+(?:thank\s+you|thanks|please)[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:that\s+(?:helped|resolved\s+it|fixed\s+it)|all\s+(?:good|set))(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:close\s+ticket|mark\s+as\s+resolved|issue\s+resolved)(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
        r"^(?:yes\s*,?\s*)?(?:that\s+helped\s+close\s+ticket)(?:\s*,?\s*(?:thank\s+you|thanks))?[!.?]*$",
    ]
    return any(re.match(p, t) for p in patterns)


def is_conversational_followup_for_held_ticket(
    text: str,
    active_ticket: Optional[Dict[str, Any]] = None,
) -> bool:
    if is_explicit_ticket_reference(text):
        return True
    t = text.strip().lower()
    if not t:
        return True
    if re.match(r"^(also|and|by the way|additionally|as well|furthermore|update:|regarding)\b", t):
        return True
    followup_phrases = (
        "while you review",
        "review this",
        "waiting",
        "when will",
        "follow up with me",
        "get back to me",
        "for updates",
        "my alternate email",
        "my email is",
        "my phone is",
        "here is",
        "receipt",
        "purchase receipt",
        "attached",
        "i am here",
        "i'm here",
        "still waiting",
        "just checking",
    )
    if any(p in t for p in followup_phrases):
        return True
    if re.match(r"^(yes|yep|yeah|ok|okay|sure|thanks|thank you)\b", t):
        return True
    return False


# -----------------------------------------------------------------------------
# Multi-Intent (from multi_intent.py @ a48a288)
# -----------------------------------------------------------------------------

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
    if not text or not text.strip():
        return None
    t = text.strip()
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


# -----------------------------------------------------------------------------
# High-Level Baseline Interpretation Pipeline
# -----------------------------------------------------------------------------

@dataclass
class BaselineTurnResult:
    turn_act: str
    primary_intent: str
    escalate: bool
    escalation_reason: Optional[str]
    sentiment: str
    urgency: str
    route: str  # "auto_rag", "escalate", "security_refusal", "resolved", "confirmation_prompt", "followup", "conditional_resolution"
    security_flags: Dict[str, bool] = field(default_factory=dict)
    referenced_ticket: Optional[str] = None
    sub_questions: List[Dict[str, str]] = field(default_factory=list)


_is_conditional_resolution = is_conditional_resolution
_is_unconditional_resolved = is_unconditional_resolved
_is_affirmative_resolution = is_affirmative_resolution
_is_conversational_followup_for_held_ticket = is_conversational_followup_for_held_ticket


def evaluate_baseline_turn(
    text: str,
    conversation_state: Optional[Dict[str, Any]] = None,
    s1_pred: Optional[Any] = None,
    verified_customer: bool = True,
) -> BaselineTurnResult:
    """Evaluate a single turn using the a48a288 regex + policy rules."""
    conversation_state = conversation_state or {}
    pending_confirmation = conversation_state.get("pending_confirmation")
    active_tickets = conversation_state.get("active_tickets", [])
    has_active_human_ticket = any(
        t.get("owner", "") != "bot" or (t.get("queue") or "").startswith("human")
        for t in active_tickets
    )
    ref_tid = extract_ticket_id_reference(text)

    # 1. Tier 0 rules
    t0 = evaluate_tier0_rules(text)
    if t0.matched:
        if t0.category == "authorization_bypass_attempt":
            return BaselineTurnResult(
                turn_act="other",
                primary_intent="authorization_bypass_attempt",
                escalate=False,
                escalation_reason=None,
                sentiment="neutral",
                urgency="normal",
                route="security_refusal",
                security_flags={"authorization_bypass_attempt": True},
            )
        if t0.category == "cross_customer_data_request":
            return BaselineTurnResult(
                turn_act="other",
                primary_intent="cross_customer_data_request",
                escalate=False,
                escalation_reason=None,
                sentiment="neutral",
                urgency="normal",
                route="security_refusal",
                security_flags={"cross_customer_data_request": True},
            )
        if t0.category == "conversation_history":
            return BaselineTurnResult(
                turn_act="new_request",
                primary_intent="conversation_history_access",
                escalate=False,
                escalation_reason=None,
                sentiment="neutral",
                urgency="normal",
                route="conversation_history",
                security_flags={"conversation_history_request": True},
            )
        if t0.escalate:
            return BaselineTurnResult(
                turn_act="new_request",
                primary_intent="other",
                escalate=True,
                escalation_reason=t0.reason,
                sentiment=t0.sentiment,
                urgency=t0.urgency,
                route="escalate",
            )
        if t0.category in ("greeting", "pleasantry"):
            return BaselineTurnResult(
                turn_act="other",
                primary_intent="other",
                escalate=False,
                escalation_reason=None,
                sentiment=t0.sentiment,
                urgency=t0.urgency,
                route="auto_rag",
            )

    # 2. Conditional resolution
    if is_conditional_resolution(text):
        return BaselineTurnResult(
            turn_act="conditional_resolution",
            primary_intent="status_inquiry",
            escalate=False,
            escalation_reason=None,
            sentiment="neutral",
            urgency="normal",
            route="conditional_resolution",
            referenced_ticket=ref_tid,
        )

    # 3. Confirmation / Resolution
    is_aff = is_affirmative_resolution(text)
    is_unc = is_unconditional_resolved(text)
    if is_aff or is_unc:
        # Check if pending confirmation or held ticket
        if has_active_human_ticket:
            return BaselineTurnResult(
                turn_act="followup_to_active_ticket",
                primary_intent="confirmation",
                escalate=False,
                escalation_reason=None,
                sentiment="neutral",
                urgency="normal",
                route="human_ticket_note",
                referenced_ticket=ref_tid,
            )
        if is_unc and not is_aff:
            return BaselineTurnResult(
                turn_act="resolution_report",
                primary_intent="confirmation",
                escalate=False,
                escalation_reason=None,
                sentiment="neutral",
                urgency="normal",
                route="confirmation_prompt",
                referenced_ticket=ref_tid,
            )
        if is_aff and pending_confirmation:
            return BaselineTurnResult(
                    turn_act="confirmation_yes",
                    primary_intent="confirmation",
                    escalate=False,
                    escalation_reason=None,
                    sentiment="neutral",
                    urgency="normal",
                    route="resolved",
                    referenced_ticket=ref_tid,
                )
            # Without pending confirmation, regex treated it as not resolving (falls through)

    # 4. Held ticket follow-up
    if has_active_human_ticket:
        active_t = active_tickets[0] if active_tickets else None
        if is_conversational_followup_for_held_ticket(text, active_t):
            return BaselineTurnResult(
                turn_act="followup_to_active_ticket",
                primary_intent="followup",
                escalate=True,
                escalation_reason="multi_turn_holding",
                sentiment="neutral",
                urgency="normal",
                route="followup",
                referenced_ticket=ref_tid,
            )

    # 5. Order lookup rule
    if matches_order_lookup(text):
        return BaselineTurnResult(
            turn_act="new_request",
            primary_intent="order_status",
            escalate=False,
            escalation_reason=None,
            sentiment="neutral",
            urgency="normal",
            route="auto_rag",
            referenced_ticket=ref_tid,
        )

    # 6. Multi-intent deconstruction
    sub_qs = detect_sub_questions(text)
    if len(sub_qs) >= 2:
        return BaselineTurnResult(
            turn_act="new_request",
            primary_intent=sub_qs[0]["intent"],
            escalate=False,
            escalation_reason=None,
            sentiment="neutral",
            urgency="normal",
            route="auto_rag",
            sub_questions=sub_qs,
            referenced_ticket=ref_tid,
        )

    # 7. Fallback / S1 / Default
    s1_intent = getattr(s1_pred, "intent", "other") if s1_pred else "other"
    s1_esc = getattr(s1_pred, "escalate", False) if s1_pred else False
    s1_sentiment = getattr(s1_pred, "sentiment", "neutral") if s1_pred else "neutral"
    s1_urgency = getattr(s1_pred, "urgency", "normal") if s1_pred else "normal"

    return BaselineTurnResult(
        turn_act="new_request",
        primary_intent=s1_intent,
        escalate=s1_esc,
        escalation_reason="s1_decision" if s1_esc else None,
        sentiment=s1_sentiment,
        urgency=s1_urgency,
        route="escalate" if s1_esc else "auto_rag",
        referenced_ticket=ref_tid,
    )
