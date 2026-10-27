"""Schemas and contracts for ticket-db-mcp tool endpoints.

Implements REQ-AGENT-010: Typed JSON contracts, schema validation, and error models.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class UpsertTicketRequest(BaseModel):
    """Input payload for upsert_ticket tool."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(..., description="Deterministic idempotency key (session_id:msg_hash)")
    customer_id: str = Field(..., description="Pseudonymous customer identifier")
    channel: str = Field(default="web", description="Inbound communication channel (e.g. web, email, chat)")
    body_redacted: str = Field(..., description="Sanitized/redacted message body")
    decision: Dict[str, Any] = Field(..., description="RouterDecision classification payload")


class UpsertTicketResponse(BaseModel):
    """Output payload for upsert_ticket tool."""

    ticket_id: str = Field(..., description="Unique ticket identifier")
    status: str = Field(..., description="Ticket status (e.g. OPEN, ESCALATED, RESOLVED_AUTO)")
    deduplicated: bool = Field(default=False, description="True if ticket was returned from existing idempotency record")


class ClaimTicketRequest(BaseModel):
    """Input payload for claim_ticket tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket ID to claim")
    agent_id: str = Field(..., description="Human operator or workflow agent ID claiming the ticket")


class ClaimTicketResponse(BaseModel):
    """Output payload for claim_ticket tool."""

    success: bool = Field(..., description="True if ticket was claimed successfully")
    error: Optional[str] = Field(default=None, description="Error code if claim failed (not_found, already_claimed)")


class ResolveTicketRequest(BaseModel):
    """Input payload for resolve_ticket tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket ID to resolve")
    agent_id: str = Field(..., description="Human operator or workflow agent ID resolving the ticket")
    resolution_notes: str = Field(default="", description="Operator resolution notes or audit explanation")


class ResolveTicketResponse(BaseModel):
    """Output payload for resolve_ticket tool."""

    status: str = Field(..., description="New status of the ticket (RESOLVED or error status)")


class ReidentifyPiiRequest(BaseModel):
    """Input payload for reidentify_pii tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket ID to unmask")
    agent_id: str = Field(..., description="Operator ID requesting unmasking (must be the active claimer)")


class ReidentifyPiiResponse(BaseModel):
    """Output payload for reidentify_pii tool."""

    unmasked_fields: Dict[str, Any] = Field(default_factory=dict, description="Dictionary of unmasked PII attributes")
    error: Optional[str] = Field(default=None, description="Error reason if unmasking failed")


def get_tool_schemas() -> Dict[str, Any]:
    """Return JSON schemas for ticket-db-mcp tools per REQ-AGENT-010."""
    return {
        "upsert_ticket": {
            "name": "upsert_ticket",
            "description": "Upsert support ticket idempotently using session_id:message_hash.",
            "parameters": UpsertTicketRequest.model_json_schema(),
            "returns": UpsertTicketResponse.model_json_schema(),
        },
        "claim_ticket": {
            "name": "claim_ticket",
            "description": "Claim an open ticket for human operator review.",
            "parameters": ClaimTicketRequest.model_json_schema(),
            "returns": ClaimTicketResponse.model_json_schema(),
        },
        "resolve_ticket": {
            "name": "resolve_ticket",
            "description": "Mark a support ticket as resolved with operator notes.",
            "parameters": ResolveTicketRequest.model_json_schema(),
            "returns": ResolveTicketResponse.model_json_schema(),
        },
        "reidentify_pii": {
            "name": "reidentify_pii",
            "description": "Audited customer PII unmasking restricted strictly to the claimed ticket operator.",
            "parameters": ReidentifyPiiRequest.model_json_schema(),
            "returns": ReidentifyPiiResponse.model_json_schema(),
        },
    }
