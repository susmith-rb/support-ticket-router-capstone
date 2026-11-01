"""Deterministic Tier 0 security intents and refusal generation.

# REQ-SAFE-002: Deterministic Tier-0 Security Intents and Authorization Bypass Refusal
# REQ-TURN-004: Deterministic Enforcement of Turn Acts and Security Refusals
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

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


def get_security_intent_reply(intent: str, text: str = "") -> str:
    """Return customer-facing refusal text for security intent."""
    if intent == "authorization_bypass_attempt":
        return AUTH_BYPASS_REFUSAL

    if intent == "cross_customer_data_request":
        t = text.lower()
        if "conversation" in t or "chat" in t or "transcript" in t:
            return CONVERSATION_CROSS_CUSTOMER_REFUSAL
        if "account" in t or "profile" in t or "address" in t:
            return ACCOUNT_CROSS_CUSTOMER_REFUSAL
        return ORDER_CROSS_CUSTOMER_REFUSAL

    return AUTH_BYPASS_REFUSAL


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
    """Emit structured WARNING log for security refusal (REQ-SAFE-002, REQ-TURN-004).

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
