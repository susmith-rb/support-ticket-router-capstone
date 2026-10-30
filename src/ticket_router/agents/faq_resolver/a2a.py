"""a2a.py — Agent-to-Agent (A2A) server entry and agent card for FaqResolverAgent.

REQ-AGENT-006: Inter-Agent A2A Handoff Protocol.
REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (forward unchanged).
Defines the A2A Agent Card and request handler so TriageRouterAgent (and Gateway)
can dispatch non-escalated factual inquiries to FaqResolverAgent.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ticket_router.agents.faq_resolver.agent import FaqResolverAgent, root_agent
from ticket_router.common.observability import get_current_traceparent

FAQ_RESOLVER_AGENT_CARD: Dict[str, Any] = {
    "name": "FaqResolverAgent",
    "version": "1.0.0",
    "description": "Bounded ReAct FAQ and order status resolution agent with mandatory grounded citations.",
    "runtime": "GKE Autopilot",
    "service_account": "sa-faq@susmith-fde-ticket-router.iam.gserviceaccount.com",
    "capabilities": [
        "faq_resolution",
        "order_status_inquiry",
        "grounded_synthesis",
    ],
    "input_schema": {
        "type": "object",
        "required": ["ticket_id", "session_id", "redacted_message"],
        "properties": {
            "ticket_id": {"type": "string", "description": "Unique ticket UUID"},
            "session_id": {"type": "string", "description": "Authenticated dialogue session ID"},
            "redacted_message": {"type": "string", "description": "PII-redacted customer message"},
            "customer_id": {"type": ["string", "null"], "description": "Verified customer ID from session claims or null"},
            "customer_verified": {"type": "boolean", "description": "True if customer identity was cryptographically verified"},
            "identity_issuer": {"type": ["string", "null"], "description": "Identity issuer of verified token"},
            "predicted_intent": {"type": "string", "description": "Intent predicted by router-s1"},
            "memory_summary": {"type": "string", "description": "Compact Memory Bank customer profile"},
            "traceparent": {"type": ["string", "null"], "description": "W3C traceparent header for distributed trace correlation"},
            "customer_context_token": {
                "type": ["string", "null"],
                "description": "KMS-signed ES256 Customer Context Token forwarded unchanged to order-lookup-mcp",
            },
            "ticket_uuid": {
                "type": ["string", "null"],
                "description": "Canonical ticket UUID for Cloud SQL persistence and escalation",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["status", "route"],
        "properties": {
            "status": {"type": "string", "enum": ["AUTO_RESOLVED", "ESCALATED"]},
            "route": {"type": "string", "enum": ["resolved", "human"]},
            "queue": {"type": "string", "description": "Assigned queue if escalated"},
            "answer": {"type": "string", "description": "Grounded answer text"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                },
            },
            "reason": {"type": "string", "description": "Handoff reason if escalated"},
            "degraded": {"type": "boolean", "description": "True if operating under partial outage"},
            "latency_ms": {"type": "number"},
            "handoff_done": {"type": "boolean", "description": "True if HumanHandoff workflow already persisted and published"},
            "calming_ack": {"type": ["string", "null"], "description": "Deterministic calming acknowledgement for user"},
        },
    },
}


def handle_a2a_request(
    payload: Dict[str, Any],
    agent: Optional[FaqResolverAgent] = None,
) -> Dict[str, Any]:
    """Process an inbound A2A handoff request from TriageRouterAgent or Agent Gateway.

    Args:
        payload: Dict conforming to FAQ_RESOLVER_AGENT_CARD['input_schema'].
        agent: Optional FaqResolverAgent instance; defaults to global root_agent.

    Returns:
        Dict conforming to FAQ_RESOLVER_AGENT_CARD['output_schema'].
    """
    active_agent = agent if agent is not None else root_agent

    ticket_id = str(payload.get("ticket_id", ""))
    session_id = str(payload.get("session_id", ""))
    # Accept either redacted_message or message
    message = str(payload.get("redacted_message") or payload.get("message") or "")
    customer_id_raw = payload.get("customer_id")
    customer_id = str(customer_id_raw) if customer_id_raw is not None else None
    customer_verified = bool(payload.get("customer_verified", False))
    identity_issuer_raw = payload.get("identity_issuer")
    identity_issuer = str(identity_issuer_raw) if identity_issuer_raw is not None else None
    predicted_intent = payload.get("predicted_intent")
    memory_summary = payload.get("memory_summary")
    traceparent = payload.get("traceparent") or get_current_traceparent()
    untrusted_customer_id = payload.get("untrusted_customer_id")
    raw_cct = payload.get("customer_context_token")
    customer_context_token: Optional[str] = (
        raw_cct if isinstance(raw_cct, str) and raw_cct != "" else None
    )
    raw_uuid = payload.get("ticket_uuid")
    ticket_uuid: Optional[str] = str(raw_uuid) if raw_uuid is not None else None

    if not ticket_id or not message:
        return {
            "status": "ESCALATED",
            "route": "human",
            "queue": "human-standard",
            "reason": "invalid_a2a_payload",
            "message": "Missing required A2A fields (ticket_id, message).",
            "degraded": False,
            "handoff_done": False,
            "calming_ack": None,
        }

    resolution = active_agent.resolve(
        ticket_id=ticket_id,
        session_id=session_id,
        message=message,
        customer_id=customer_id,
        customer_verified=customer_verified,
        identity_issuer=identity_issuer,
        predicted_intent=predicted_intent,
        memory_summary=memory_summary,
        traceparent=traceparent,
        untrusted_customer_id=untrusted_customer_id,
        customer_context_token=customer_context_token,
        ticket_uuid=ticket_uuid,
    )

    return resolution.to_dict()
