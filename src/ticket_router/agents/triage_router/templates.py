"""Calming acknowledgement templates for human escalation handoffs.

# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
"""

from __future__ import annotations

from typing import Dict, Optional

# Expected SLA response ETAs per queue tier (REQ-AGENT-005)
SLA_ETAS: Dict[str, str] = {
    "human-urgent": "15 minutes",
    "human-standard": "2 hours",
    "human-low": "4 hours",
}

# Deterministic fallback templates
CALMING_ACK_TEMPLATES: Dict[str, str] = {
    "urgent": (
        "Thank you for contacting us. We understand this requires prompt attention, and we have created ticket "
        "#{ticket_id} for you. A specialist has been prioritized to assist you and will respond within {sla_eta}."
    ),
    "standard": (
        "Thank you for reaching out to support. We have logged ticket #{ticket_id} and our support team "
        "is reviewing your request. A representative will get back to you within {sla_eta}."
    ),
    "low": (
        "Thank you for your inquiry. We have recorded ticket #{ticket_id}. Our customer care team will "
        "follow up with you within {sla_eta}."
    ),
    "abuse_boundary": (
        "Thank you for reaching out. We have logged ticket #{ticket_id}. To ensure a respectful environment "
        "for our team, your inquiry has been routed to a human representative who will review your request "
        "and reply within {sla_eta}."
    ),
}


def get_sla_eta(queue: str) -> str:
    """Return the expected SLA response ETA for a given queue tier."""
    return SLA_ETAS.get(queue, "2 hours")


def render_calming_acknowledgement(
    queue: str,
    ticket_id: str,
    category: Optional[str] = None,
    custom_eta: Optional[str] = None,
) -> str:
    """Render a deterministic calming acknowledgement message (REQ-AGENT-005).

    Guarantees:
    - Contains the unique ticket_id.
    - Specifies the expected SLA response ETA ('15 minutes' for urgent, '2 hours' for standard).
    - Uses a respectful boundary-setting template for abusive interactions.
    """
    sla_eta = custom_eta or get_sla_eta(queue)

    if category == "abuse":
        template = CALMING_ACK_TEMPLATES["abuse_boundary"]
    elif queue == "human-urgent":
        template = CALMING_ACK_TEMPLATES["urgent"]
    elif queue == "human-low":
        template = CALMING_ACK_TEMPLATES["low"]
    else:
        template = CALMING_ACK_TEMPLATES["standard"]

    return template.format(ticket_id=ticket_id, sla_eta=sla_eta)


OUT_OF_SCOPE_TEMPLATE = (
    "Thank you for reaching out (ref #{ticket_id}). I am the Global Retail Hub customer "
    "support assistant and can only help with orders, shipping policies, returns and refunds, "
    "or account and payment questions. I'm unable to assist with unrelated topics, but please "
    "let me know if you need help with a Global Retail Hub order or account!"
)


def render_out_of_scope_refusal(ticket_id: str) -> str:
    """Render a deterministic polite refusal for out-of-scope customer inquiries (REQ-AGENT-022)."""
    return OUT_OF_SCOPE_TEMPLATE.format(ticket_id=ticket_id)


# REQ-AGENT-024: Conversational Greeting and Pleasantry Handling
CONVERSATIONAL_GREETING_TEMPLATE = (
    "Hello! I'm the Global Retail Hub Support Assistant. I can help you check your "
    "recent orders, track a shipment, or answer questions about shipping policies, "
    "returns and refunds, or account and payment billing. How can I help you today?"
)

CONVERSATIONAL_PLEASANTRY_TEMPLATE = (
    "You're welcome! Please let me know if there's anything else I can help you "
    "with regarding your Global Retail Hub orders or account."
)


def render_conversational_reply(category: Optional[str] = None) -> str:
    """Render a deterministic friendly welcome or closing reply (REQ-AGENT-024)."""
    if category == "pleasantry":
        return CONVERSATIONAL_PLEASANTRY_TEMPLATE
    return CONVERSATIONAL_GREETING_TEMPLATE


# REQ-MA-007 / REQ-AGENT-028: Ingress Payment Card Data Block Reply (TDD §4.2 C4)
CARD_DATA_BLOCKED_REPLY = (
    "For your security we can't accept card numbers in chat. "
    "Please remove the card number and send your message again; "
    "for payment issues, use the secure payment page."
)


def render_card_data_blocked_reply() -> str:
    """Render a deterministic security response for customer prompts containing payment card data."""
    return CARD_DATA_BLOCKED_REPLY

