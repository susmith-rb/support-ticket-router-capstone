"""Deterministic Tier 0 security intents and refusal generation.

# REQ-SAFE-002: Deterministic Tier-0 Security Intents and Authorization Bypass Refusal
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Canonical refusal replies
ORDER_CROSS_CUSTOMER_REFUSAL = (
    "I can help you access your own orders, but I cannot provide other customers' order information. "
    "If you need assistance with one of your orders, I can look it up using your authorized account."
)

CONVERSATION_CROSS_CUSTOMER_REFUSAL = (
    "You can only view your own conversation history. Access to other users' conversations is not permitted."
)

ACCOUNT_CROSS_CUSTOMER_REFUSAL = (
    "I can help you access your own account information, but I cannot provide other customers' account information. "
    "If you need assistance with your account, I can look it up using your authorized account."
)

AUTH_BYPASS_REFUSAL = (
    "I can't skip or change authorization checks. "
    "I can help with your own account once you're signed in."
)

# Noun groups
_ORDER_NOUNS = r"(?:orders?|purchases?|packages?|shipments?|deliveries?|items?|tracking|receipts?)"
_CONV_NOUNS = r"(?:conversations?|chats?|interactions?|transcripts?|messages?|history|logs?|discussions?)"
_ACCOUNT_NOUNS = r"(?:accounts?|profiles?|addresses?|tickets?|cards?|credentials?|details?|information|data)"
_ALL_TARGET_NOUNS = rf"(?:{_ORDER_NOUNS}|{_CONV_NOUNS}|{_ACCOUNT_NOUNS})"

# Cross-customer patterns
_CROSS_CUSTOMER_PHRASES = [
    # orders/conversations/data of other customers/users
    rf"\b(?:{_ALL_TARGET_NOUNS})\s+(?:of|for|from|belonging\s+to)\s+(?:other\s+|another\s+|all\s+(?:other\s+)?)(?:customers?|users?|people|accounts?|persons?)\b",
    rf"\b(?:other|another|everyone(?:'s)?|all)\s+(?:customers?|users?|people|accounts?)(?:'s)?\s+{_ALL_TARGET_NOUNS}\b",
    rf"\b(?:show|give|fetch|get|list|display|find|provide|see|view|pull\s+up|access)\s+(?:me\s+)?(?:all\s+)?(?:other\s+|another\s+|someone\s+else(?:'s)?|everyone(?:'s)?|all\s+customers?(?:'s)?)\s+{_ALL_TARGET_NOUNS}\b",
    rf"\b(?:show|give|fetch|get|list|display|find|provide|see|view|pull\s+up|access)\s+(?:me\s+)?{_ALL_TARGET_NOUNS}\s+(?:of|for|from)\s+(?:other\s+|another\s+|someone\s+else|everyone|all)\s*(?:customers?|users?|people|accounts?)?\b",
    r"\ball\s+(?:chat\s+logs|support\s+tickets|customer\s+accounts|customer\s+orders)\b",
    r"\bgive\s+me\s+orders\s+of\s+other\s+customers\b",
    r"\borders\s+of\s+other\s+customers\b",
]

_CROSS_REGEXES = [re.compile(p, re.IGNORECASE) for p in _CROSS_CUSTOMER_PHRASES]

# Authorization bypass and administrative privilege impersonation
_AUTH_BYPASS_PATTERNS = [
    # Direct requests to bypass/skip authorization or verification
    r"\b(?:skip|bypass|disable|override|ignore|circumvent|turn\s+off)\s+(?:the\s+)?(?:authorization|auth|verification|security|identity|authentication)\s*(?:checks?|flow|rules?|layer)?\b",
    r"\b(?:you\s+can\s+)?(?:skip|bypass|disable)\s+(?:the\s+)?(?:authorization|auth|verification|security|identity)\s*checks?\b",
    r"\bauthorization\s+bypass\b",
    r"\bbypass\s+(?:the\s+)?(?:auth|authorization|verification|authentication|security\s+check)\b",
    # Claims of admin identity attempting to skip checks or demand root/admin access
    r"\b(?:i\s+am|i'm|im|acting\s+as)\s+(?:an?\s+)?(?:administrator|admin|system\s+operator|super\s*user|root)\b",
]

_AUTH_BYPASS_REGEXES = [re.compile(p, re.IGNORECASE) for p in _AUTH_BYPASS_PATTERNS]


def is_cross_customer_data_request(
    text: str,
    verified_customer_id: Optional[str] = None,
) -> bool:
    """Detect requests for another customer's or all customers' data (REQ-SAFE-002).

    Pure detector function.
    """
    if not text or not text.strip():
        return False

    t = text.strip().lower()
    t = re.sub(r"@user\b", "", t)

    # 1. Matches generic cross-customer requests
    if any(r.search(t) for r in _CROSS_REGEXES):
        return True

    # 2. Specific customer ID reference differing from verified caller
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
    """Detect claims of administrative role or requests to skip security checks (REQ-SAFE-002).

    Pure detector function.
    """
    if not text or not text.strip():
        return False

    t = text.strip().lower()
    return any(r.search(t) for r in _AUTH_BYPASS_REGEXES)


def get_security_intent_reply(intent: str, text: str = "") -> str:
    """Return customer-facing refusal text for security intent."""
    if intent == "authorization_bypass_attempt":
        return AUTH_BYPASS_REFUSAL

    if intent == "cross_customer_data_request":
        t = text.lower()
        if re.search(rf"\b{_CONV_NOUNS}\b", t) and not re.search(rf"\b{_ORDER_NOUNS}\b", t):
            return CONVERSATION_CROSS_CUSTOMER_REFUSAL
        if re.search(r"\b(?:account|profile|address|ticket|credential)s?\b", t) and not re.search(rf"\b{_ORDER_NOUNS}\b", t):
            return ACCOUNT_CROSS_CUSTOMER_REFUSAL
        return ORDER_CROSS_CUSTOMER_REFUSAL

    return AUTH_BYPASS_REFUSAL


def detect_security_intent(
    text: str,
    verified_customer_id: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Evaluate deterministic Tier 0 security intents in precedence order.

    Returns:
        (intent_name, refusal_reply) or None
    """
    if not text or not text.strip():
        return None

    if is_authorization_bypass_attempt(text):
        return (
            "authorization_bypass_attempt",
            get_security_intent_reply("authorization_bypass_attempt", text),
        )

    if is_cross_customer_data_request(text, verified_customer_id=verified_customer_id):
        return (
            "cross_customer_data_request",
            get_security_intent_reply("cross_customer_data_request", text),
        )

    return None


def emit_security_refusal_log(
    *,
    trace_id: Optional[str] = None,
    traceparent: Optional[str] = None,
    session_id: str,
    channel: str = "web",
    customer_verified: bool = False,
    customer_id: Optional[str] = None,
    security_intent: Optional[str] = None,
    reason: Optional[str] = None,
    message_text: str = "",
) -> None:
    """Emit structured WARNING log for security refusal (REQ-SAFE-002).

    Invariant: Zero customer tokens; sha256 + length only.
    """
    eff_trace = traceparent or trace_id or "unknown"
    eff_reason = security_intent or reason or "security_refusal"
    msg_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
    log_payload: Dict[str, Any] = {
        "event": "security_refusal",
        "severity": "WARNING",
        "trace_id": eff_trace,
        "session_id": session_id,
        "channel": channel,
        "customer_verified": bool(customer_verified),
        "customer_id": customer_id if customer_verified else None,
        "security_intent": eff_reason,
        "reason": eff_reason,
        "message_sha256": msg_hash,
        "message_length": len(message_text),
    }
    logger.warning(json.dumps(log_payload, separators=(",", ":")))
