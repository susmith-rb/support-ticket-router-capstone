"""FastAPI server for human-queue agent service on Cloud Run.

# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-031: Escalation Event Consumer (Pub/Sub push -> human-queue)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.store.interfaces import TicketRepository

logger = logging.getLogger("ticket_router.agents.human_queue.server")

router = APIRouter()

REQUIRED_ESCALATED_SCHEMA = "ticket.escalated/v1"
ALLOWED_ESCALATED_BY = ("triage_router", "faq_resolver")
FORBIDDEN_PII_KEYS = {
    "customer_id",
    "message",
    "message_text",
    "body",
    "body_redacted",
    "token",
    "tokens",
    "redaction_map",
}


class PubSubPushMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: str = Field(..., description="Base64-encoded UTF-8 JSON message data")
    attributes: Optional[Dict[str, Any]] = None
    messageId: Optional[str] = None
    publishTime: Optional[str] = None
    orderingKey: Optional[str] = None


class PubSubPushEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: PubSubPushMessage
    subscription: Optional[str] = None


class ClaimPayload(BaseModel):
    ticket_id: str = Field(..., description="Ticket UUID to claim")
    operator_id: str = Field(..., description="Operator LDAP or ID")


class ResolvePayload(BaseModel):
    ticket_id: str = Field(..., description="Ticket UUID to resolve")
    operator_id: str = Field(..., description="Operator LDAP or ID")
    resolution_notes: str = Field(default="", description="Operator resolution notes")


class ReidentifyPayload(BaseModel):
    ticket_id: str = Field(..., description="Ticket UUID to unmask")
    operator_id: str = Field(..., description="Operator LDAP or ID (must be active claimer)")


def get_agent(request: Request) -> HumanQueueAgent:
    """Dependency retrieving configured HumanQueueAgent from app state."""
    try:
        return request.app.state.agent
    except AttributeError as err:
        raise RuntimeError("HumanQueueAgent not configured on app.state") from err


def get_ticket_repo(request: Request) -> TicketRepository:
    """Dependency retrieving configured TicketRepository from app state."""
    if hasattr(request.app.state, "ticket_repo") and request.app.state.ticket_repo is not None:
        return request.app.state.ticket_repo
    try:
        return request.app.state.agent.ticket_repo
    except AttributeError as err:
        raise RuntimeError("TicketRepository not available on app.state") from err


def verify_push_auth(request: Request) -> Dict[str, Any]:
    """Verify inbound Pub/Sub push OIDC authentication (REQ-AGENT-031)."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected 'Bearer <token>')",
        )
    token = auth_header[7:].strip()
    expected_aud = os.environ.get("PUSH_AUDIENCE")
    expected_email = os.environ.get("PUSH_INVOKER_EMAIL")

    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is not None:
        try:
            claims = verifier(token, expected_aud)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"OIDC verification failed: {exc}") from exc
    else:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token

        try:
            claims = google_id_token.verify_oauth2_token(
                token,
                GoogleAuthRequest(),
                audience=expected_aud if expected_aud else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"OIDC verification failed: {exc}") from exc

    iss = claims.get("iss", "")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail=f"Invalid OIDC issuer: {iss}")

    if expected_aud and claims.get("aud") != expected_aud:
        raise HTTPException(status_code=401, detail="OIDC audience mismatch")

    email = claims.get("email", "")
    if expected_email and email != expected_email:
        raise HTTPException(status_code=401, detail=f"Unauthorized invoker email: {email}")

    if not claims.get("email_verified", False):
        raise HTTPException(status_code=401, detail="OIDC email not verified")

    return claims


def validate_escalated_payload(data: Any) -> Dict[str, Any]:
    """Validate ticket.escalated/v1 JSON schema and assert absence of PII (REQ-AGENT-031)."""
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")
    if data.get("schema") != REQUIRED_ESCALATED_SCHEMA:
        raise ValueError(
            f"Invalid schema: expected '{REQUIRED_ESCALATED_SCHEMA}', got '{data.get('schema')}'"
        )
    if not data.get("ticket_id") or not isinstance(data["ticket_id"], str):
        raise ValueError("Missing or invalid 'ticket_id'")
    if "ticket_uuid" in data and (not data["ticket_uuid"] or not isinstance(data["ticket_uuid"], str)):
        raise ValueError("Invalid 'ticket_uuid' (must be non-empty string)")
    if not data.get("queue") or not isinstance(data["queue"], str):
        raise ValueError("Missing or invalid 'queue'")
    if not data.get("priority") or not isinstance(data["priority"], str):
        raise ValueError("Missing or invalid 'priority'")
    if not data.get("reason") or not isinstance(data["reason"], str):
        raise ValueError("Missing or invalid 'reason'")
    if data.get("escalated_by") not in ALLOWED_ESCALATED_BY:
        raise ValueError(f"Invalid 'escalated_by': {data.get('escalated_by')}")
    if not isinstance(data.get("degraded"), bool):
        raise ValueError("Missing or invalid 'degraded' (must be boolean)")
    if not data.get("ts") or not isinstance(data["ts"], str):
        raise ValueError("Missing or invalid 'ts' timestamp")
    if "traceparent" in data and data["traceparent"] is not None and not isinstance(data["traceparent"], str):
        raise ValueError("'traceparent' must be string or null")

    found_forbidden = FORBIDDEN_PII_KEYS.intersection(data.keys())
    if found_forbidden:
        raise ValueError(f"Forbidden PII keys found in escalation payload: {found_forbidden}")

    return data


@router.get("/health")
@router.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/")
def root(agent: HumanQueueAgent = Depends(get_agent)) -> Dict[str, Any]:
    return {
        "service": "human-queue",
        "status": "running",
        "depths": agent.queue_depths(),
    }


@router.get("/queue")
@router.get("/tickets")
def list_queue(
    queue: Optional[str] = Query(None, description="Optional queue name filter (e.g. human-urgent)"),
    limit: int = Query(50, ge=1, le=200, description="Max tickets to return"),
    agent: HumanQueueAgent = Depends(get_agent),
) -> List[Dict[str, Any]]:
    """List open tickets sorted by priority (urgent before standard, FIFO)."""
    tickets = agent.list_open_queue(limit=limit, queue=queue)
    return [
        {
            "ticket_id": str(t.ticket_id),
            "customer_id": t.customer_id,
            "assigned_queue": t.assigned_queue,
            "status": t.status,
            "assigned_to": t.assigned_to,
            "body_redacted": t.body_redacted,
            "created_at": t.created_at.isoformat(),
        }
        for t in tickets
    ]


@router.get("/queue/depths")
def queue_depths(agent: HumanQueueAgent = Depends(get_agent)) -> Dict[str, int]:
    return agent.queue_depths()


@router.post("/claim")
def claim(
    payload: ClaimPayload,
    agent: HumanQueueAgent = Depends(get_agent),
) -> Dict[str, Any]:
    result = agent.claim_ticket(ticket_id=payload.ticket_id, operator_id=payload.operator_id)
    if not result.get("success"):
        err = result.get("error", "claim_failed")
        if err == "not_found":
            raise HTTPException(status_code=404, detail="Ticket not found")
        raise HTTPException(status_code=409, detail=f"Cannot claim ticket: {err}")
    return result


@router.post("/resolve")
def resolve(
    payload: ResolvePayload,
    agent: HumanQueueAgent = Depends(get_agent),
) -> Dict[str, Any]:
    result = agent.resolve_ticket(
        ticket_id=payload.ticket_id,
        operator_id=payload.operator_id,
        resolution_notes=payload.resolution_notes,
    )
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Ticket not found")
    return result


@router.post("/reidentify")
def reidentify(
    payload: ReidentifyPayload,
    agent: HumanQueueAgent = Depends(get_agent),
) -> Dict[str, Any]:
    try:
        return agent.reidentify_pii(
            ticket_id=payload.ticket_id,
            operator_id=payload.operator_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/pubsub/escalations")
async def handle_pubsub_escalation(
    request: Request,
    envelope: PubSubPushEnvelope,
    claims: Dict[str, Any] = Depends(verify_push_auth),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> Response:
    """Escalation Event Consumer handling Pub/Sub push messages (REQ-AGENT-031)."""
    try:
        raw_bytes = base64.b64decode(envelope.message.data)
        raw_data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed Pub/Sub message data: {exc}") from exc

    try:
        payload = validate_escalated_payload(raw_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ticket_id = payload["ticket_id"]
    ticket_uuid_str = payload.get("ticket_uuid")

    # Accept legacy messages without ticket_uuid by acking with 204 and warning (REQ-AGENT-031)
    if not ticket_uuid_str:
        logger.warning(
            json.dumps(
                {
                    "event": "legacy_escalation_missing_ticket_uuid",
                    "ticket_id": ticket_id,
                },
                separators=(",", ":"),
            )
        )
        return Response(status_code=204)

    try:
        target_uuid = UUID(ticket_uuid_str)
    except (ValueError, TypeError):
        target_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{ticket_uuid_str}")

    # Check if ticket exists in repository; if not, return 204 and log ticket_not_found (REQ-AGENT-031)
    ticket = ticket_repo.get_ticket(target_uuid)
    if ticket is None:
        logger.warning(
            json.dumps(
                {
                    "event": "ticket_not_found",
                    "ticket_id": ticket_id,
                    "ticket_uuid": str(target_uuid),
                },
                separators=(",", ":"),
            )
        )
        return Response(status_code=204)

    # Idempotency check: duplicate messages return 204 without a second event (REQ-AGENT-031)
    existing_events = ticket_repo.list_events(target_uuid)
    for ev in existing_events:
        if ev.event_type == "queued_for_review":
            return Response(status_code=204)

    # Record one idempotent queued_for_review event
    ticket_repo.append_event(
        ticket_id=target_uuid,
        event_type="queued_for_review",
        actor=payload["escalated_by"],
        payload={
            "queue": payload["queue"],
            "priority": payload["priority"],
            "reason": payload["reason"],
            "degraded": payload["degraded"],
            "traceparent": payload.get("traceparent"),
        },
    )
    return Response(
        content=json.dumps({"status": "queued", "ticket_id": ticket_id, "ticket_uuid": str(target_uuid)}),
        status_code=200,
        media_type="application/json",
    )


def create_app(
    ticket_repo: Optional[TicketRepository] = None,
    redaction_store: Optional[Dict[str, Dict[str, Any]]] = None,
    token_verifier: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
) -> FastAPI:
    """Create FastAPI application for human-queue service on Cloud Run."""
    if ticket_repo is None:
        allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")
        if allow_fake:
            from ticket_router.store.fake import FakeTicketRepo

            ticket_repo = FakeTicketRepo()
        else:
            raise RuntimeError(
                "TicketRepository is required to create human-queue app (or set ALLOW_FAKE_REPOS=1)"
            )

    application = FastAPI(
        title="human-queue",
        version="0.1.0",
        description="HumanQueueAgent service for ticket claiming, priority queue management, and audited PII unmasking.",
    )
    application.state.agent = HumanQueueAgent(
        ticket_repo=ticket_repo,
        redaction_store=redaction_store,
    )
    application.state.ticket_repo = ticket_repo
    application.state.token_verifier = token_verifier
    application.include_router(router)

    # Concurrently created purge router
    try:
        from ticket_router.agents.human_queue import purge

        application.include_router(purge.router)
    except ImportError:
        pass

    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
