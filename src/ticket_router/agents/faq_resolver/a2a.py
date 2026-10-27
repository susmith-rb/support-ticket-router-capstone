"""a2a.py — Agent-to-Agent (A2A) server entry and agent card for FaqResolverAgent.

REQ-AGENT-006: Inter-Agent A2A Handoff Protocol.
Defines the A2A Agent Card and request handler so TriageRouterAgent (and Gateway)
can dispatch non-escalated factual inquiries to FaqResolverAgent.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ticket_router.agents.faq_resolver.agent import FaqResolverAgent, root_agent

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
        "required": ["ticket_id", "session_id", "redacted_message", "customer_id"],
        "properties": {
            "ticket_id": {"type": "string", "description": "Unique ticket UUID"},
            "session_id": {"type": "string", "description": "Authenticated dialogue session ID"},
            "redacted_message": {"type": "string", "description": "PII-redacted customer message"},
            "customer_id": {"type": "string", "description": "Verified customer ID from session claims"},
            "predicted_intent": {"type": "string", "description": "Intent predicted by router-s1"},
            "memory_summary": {"type": "string", "description": "Compact Memory Bank customer profile"},
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
    customer_id = str(payload.get("customer_id", ""))
    predicted_intent = payload.get("predicted_intent")
    memory_summary = payload.get("memory_summary")
    traceparent = payload.get("traceparent")
    untrusted_customer_id = payload.get("untrusted_customer_id")

    if not ticket_id or not message or not customer_id:
        return {
            "status": "ESCALATED",
            "route": "human",
            "queue": "human-standard",
            "reason": "invalid_a2a_payload",
            "message": "Missing required A2A fields (ticket_id, message, customer_id).",
            "degraded": False,
        }

    resolution = active_agent.resolve(
        ticket_id=ticket_id,
        session_id=session_id,
        message=message,
        customer_id=customer_id,
        predicted_intent=predicted_intent,
        memory_summary=memory_summary,
        traceparent=traceparent,
        untrusted_customer_id=untrusted_customer_id,
    )

    return resolution.to_dict()
