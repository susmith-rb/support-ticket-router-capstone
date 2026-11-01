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


class ResolveCustomerRequest(BaseModel):
    """Input payload for resolve_customer tool."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(..., description="OIDC token issuer URL (e.g. https://securetoken.google.com/<project>)")
    subject: str = Field(..., description="IdP subject identifier (localId / uid)")


class ResolveCustomerResponse(BaseModel):
    """Output payload for resolve_customer tool."""

    customer_id: Optional[str] = Field(default=None, description="Mapped internal customer ID, or null if unlinked")
    status: str = Field(..., description="Customer status: active, suspended, or unlinked")
    linked: bool = Field(..., description="True if identity is mapped to a customer account")


class SaveRedactionMapRequest(BaseModel):
    """Input payload for save_redaction_map tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket identifier")
    customer_id: str = Field(..., description="Customer identifier")
    envelope_b64: str = Field(..., description="Base64-encoded KMS envelope")
    key_id: str = Field(default="kms-envelope-v1", description="Identifier of the key or envelope")


class SaveRedactionMapResponse(BaseModel):
    """Output payload for save_redaction_map tool."""

    saved: bool = Field(default=True, description="True if redaction map saved successfully")


class GetActiveTicketRequest(BaseModel):
    """Input payload for get_active_ticket_for_session tool (REQ-AGENT-033)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., description="Dialogue session identifier")
    customer_id: Optional[str] = Field(
        default=None, description="Optional customer identifier for tenant scoping"
    )


class GetActiveTicketResponse(BaseModel):
    """Output payload for get_active_ticket_for_session tool (REQ-AGENT-033)."""

    ticket_id: Optional[str] = Field(
        default=None, description="Active holding ticket ID or null if none active"
    )
    status: Optional[str] = Field(default=None, description="Ticket status")
    assigned_queue: Optional[str] = Field(
        default=None, description="Assigned human queue"
    )
    customer_id: Optional[str] = Field(
        default=None, description="Customer identifier"
    )
    ticket: Optional[Dict[str, Any]] = Field(
        default=None, description="Full ticket payload or null"
    )


class GetPendingConfirmationRequest(BaseModel):
    """Input payload for get_pending_confirmation tool (REQ-TURN-005)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., description="Dialogue session identifier")
    customer_id: Optional[str] = Field(
        default=None, description="Optional customer identifier for tenant scoping"
    )


class GetPendingConfirmationResponse(BaseModel):
    """Output payload for get_pending_confirmation tool (REQ-TURN-005)."""

    ticket_id: Optional[str] = Field(
        default=None, description="Pending confirmation ticket ID or null if none pending"
    )
    status: Optional[str] = Field(default=None, description="Ticket status")
    issue_reference: Optional[str] = Field(
        default=None, description="Canonical customer-facing issue reference"
    )
    customer_id: Optional[str] = Field(
        default=None, description="Customer identifier"
    )
    ticket: Optional[Dict[str, Any]] = Field(
        default=None, description="Full ticket payload or null"
    )


class AppendCustomerFollowupRequest(BaseModel):
    """Input payload for append_customer_followup tool (REQ-AGENT-033)."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket identifier")
    body_redacted: str = Field(..., description="Sanitized/redacted follow-up message body")
    customer_id: Optional[str] = Field(
        default=None, description="Customer identifier for tenant scoping"
    )
    traceparent: Optional[str] = Field(
        default=None, description="Distributed trace context"
    )
    message_hash: Optional[str] = Field(
        default=None, description="Optional precomputed message hash for idempotency"
    )


class AppendCustomerFollowupResponse(BaseModel):
    """Output payload for append_customer_followup tool (REQ-AGENT-033)."""

    success: bool = Field(default=True, description="True if followup event was recorded")
    event_id: str = Field(..., description="Audit event identifier")
    deduplicated: bool = Field(
        default=False, description="True if followup was deduplicated"
    )


class UpdateTicketStatusRequest(BaseModel):
    """Input payload for update_ticket_status tool (REQ-STORE-011, REQ-LIFE-002)."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket identifier or UUID")
    from_status: str = Field(..., description="Expected current status before transition")
    to_status: str = Field(..., description="Target status")
    actor: str = Field(..., description="Actor making the status transition")
    reason: str = Field(..., description="Reason code or audit justification")
    idempotency_key: str = Field(..., description="Deterministic idempotency key for this transition")
    trace: Optional[str] = Field(default=None, description="Distributed trace context or trace ID")
    customer_id: Optional[str] = Field(
        default=None,
        description="Optional customer identifier; must match verified identity header X-Customer-ID",
    )


class RecordConfirmationRequestedRequest(BaseModel):
    """Input payload for record_resolution_confirmation_requested tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket identifier or UUID")
    prompt: Optional[str] = Field(
        default=None, description="Confirmation prompt text asked to customer"
    )


class RecordConfirmationRequestedResponse(BaseModel):
    """Output payload for record_resolution_confirmation_requested tool."""

    success: bool = Field(default=True, description="True if marker was recorded")
    ticket_id: str = Field(..., description="Target ticket identifier")


class UpdateTicketStatusResponse(BaseModel):
    """Output payload for update_ticket_status tool (REQ-STORE-011, REQ-LIFE-002)."""

    success: bool = Field(..., description="True if status updated or already in target state")
    ticket_id: str = Field(..., description="Target ticket identifier")
    status: str = Field(..., description="Current status of the ticket")
    error: Optional[str] = Field(default=None, description="Error code if transition failed")
    idempotent: bool = Field(default=False, description="True if request was an idempotent replay")


class GetTicketRequest(BaseModel):
    """Input payload for get_ticket tool."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Ticket identifier or UUID")


class GetTicketResponse(BaseModel):
    """Output payload for get_ticket tool."""

    ticket: Optional[Dict[str, Any]] = Field(default=None, description="Ticket dictionary")
    ticket_id: Optional[str] = Field(default=None, description="Ticket identifier")
    status: Optional[str] = Field(default=None, description="Current status")


class CustomerTicketSummary(BaseModel):
    """Projection of recent customer ticket metadata (REQ-HIST-003).

    Excludes body_redacted, message text, and internal decision payload.
    """

    model_config = ConfigDict(extra="forbid")

    issue_reference: str = Field(..., description="Customer-facing ticket reference (e.g. TICK-...)")
    status: str = Field(..., description="Ticket status (under review for security/fraud)")
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    category: str = Field(..., description="Ticket category")


class ListCustomerTicketsRequest(BaseModel):
    """Input payload for list_customer_tickets tool (REQ-HIST-003)."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., description="Customer identifier")
    limit: int = Field(default=5, ge=1, le=50, description="Max tickets to return (default 5)")


class ListCustomerTicketsResponse(BaseModel):
    """Output payload for list_customer_tickets tool (REQ-HIST-003)."""

    tickets: list[CustomerTicketSummary] = Field(
        default_factory=list, description="Recent customer tickets newest first"
    )



def get_tool_schemas() -> Dict[str, Any]:
    """Return JSON schemas for ticket-db-mcp tools per REQ-AGENT-010."""
    return {
        "upsert_ticket": {
            "name": "upsert_ticket",
            "description": "Upsert support ticket idempotently using session_id:message_hash.",
            "parameters": UpsertTicketRequest.model_json_schema(),
            "returns": UpsertTicketResponse.model_json_schema(),
        },
        "save_redaction_map": {
            "name": "save_redaction_map",
            "description": "Store KMS envelope-encrypted PII redaction map (REQ-STORE-009).",
            "parameters": SaveRedactionMapRequest.model_json_schema(),
            "returns": SaveRedactionMapResponse.model_json_schema(),
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
        "resolve_customer": {
            "name": "resolve_customer",
            "description": "Resolve an IdP (issuer, subject) identity pair to an internal customer ID.",
            "parameters": ResolveCustomerRequest.model_json_schema(),
            "returns": ResolveCustomerResponse.model_json_schema(),
        },
        "get_active_ticket_for_session": {
            "name": "get_active_ticket_for_session",
            "description": "Retrieve the most recent active holding ticket for a conversation session (REQ-AGENT-033).",
            "parameters": GetActiveTicketRequest.model_json_schema(),
            "returns": GetActiveTicketResponse.model_json_schema(),
        },
        "append_customer_followup": {
            "name": "append_customer_followup",
            "description": "Append a customer follow-up audit event to an active ticket (REQ-AGENT-033).",
            "parameters": AppendCustomerFollowupRequest.model_json_schema(),
            "returns": AppendCustomerFollowupResponse.model_json_schema(),
        },
        "update_ticket_status": {
            "name": "update_ticket_status",
            "description": "Update ticket lifecycle status with deterministic backend validation (REQ-STORE-011, REQ-LIFE-002).",
            "parameters": UpdateTicketStatusRequest.model_json_schema(),
            "returns": UpdateTicketStatusResponse.model_json_schema(),
        },
        "record_resolution_confirmation_requested": {
            "name": "record_resolution_confirmation_requested",
            "description": "Record that an affirmative resolution confirmation prompt was asked for a ticket (REQ-LIFE-002).",
            "parameters": RecordConfirmationRequestedRequest.model_json_schema(),
            "returns": RecordConfirmationRequestedResponse.model_json_schema(),
        },
        "get_ticket": {
            "name": "get_ticket",
            "description": "Retrieve ticket details by identifier or UUID.",
            "parameters": GetTicketRequest.model_json_schema(),
            "returns": GetTicketResponse.model_json_schema(),
        },
        "list_customer_tickets": {
            "name": "list_customer_tickets",
            "description": "List recent ticket references and statuses for a customer (REQ-HIST-003).",
            "parameters": ListCustomerTicketsRequest.model_json_schema(),
            "returns": ListCustomerTicketsResponse.model_json_schema(),
        },
        "get_pending_confirmation": {
            "name": "get_pending_confirmation",
            "description": "Retrieve pending resolution confirmation ticket for a dialogue session (REQ-TURN-005).",
            "parameters": GetPendingConfirmationRequest.model_json_schema(),
            "returns": GetPendingConfirmationResponse.model_json_schema(),
        },
    }
