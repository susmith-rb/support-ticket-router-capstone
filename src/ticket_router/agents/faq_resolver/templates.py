"""Customer-facing string templates and escalation messages for FaqResolverAgent.

# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-028: Grounded Static-Passage Fallback When Answer Generation Stalls
"""

from __future__ import annotations

# Escalation messages returned during degradation, policy or error events
ESCALATE_ACCURACY_MESSAGE = (
    "To ensure complete accuracy, I've escalated your ticket to our support specialists."
)

ESCALATE_SECURITY_MESSAGE = (
    "To ensure your issue is safely and accurately handled, I have escalated your ticket to our customer support specialists."
)

ESCALATE_TECHNICAL_DELAY_MESSAGE = (
    "Our system is temporarily experiencing technical delays. A support specialist will follow up with you."
)

ESCALATE_BUDGET_EXCEEDED_PRE_MESSAGE = (
    "Turn budget exceeded before processing."
)

ESCALATE_ACCOUNT_VERIFICATION_MESSAGE = (
    "Inquiry routed to human specialist for account verification."
)

ESCALATE_KB_DEGRADED_MESSAGE = (
    "Our automated knowledge base is temporarily degraded. A support specialist will assist you."
)

ESCALATE_BUDGET_EXCEEDED_RETRIEVAL_MESSAGE = (
    "Turn budget exceeded during knowledge retrieval."
)

FEEDBACK_CITATION_REJECTED_TEMPLATE = (
    "Citation validation rejected previous response: {reason} ({details}). "
    "You MUST supply at least one citation with a quote verbatim present in the retrieved articles."
)

# Test/Fake defaults
FAKE_RETURN_POLICY_ANSWER = "You can return items anytime you feel like it."
DEFAULT_ORDER_STATUS_TEMPLATE = "Your order {order_id} is currently {status}. {answer}"
