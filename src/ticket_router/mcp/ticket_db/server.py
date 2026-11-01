"""FastAPI server for ticket-db-mcp service on Cloud Run.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification

Cloud Run Front Door Auth:
Cloud Run IAM (roles/run.invoker) handles authentication and authorization at the ingress proxy.
This service trusts Cloud Run's front door for token verification, and inspects caller identity
from 'X-Serverless-Authorization' or 'Authorization' Bearer JWT claims for structured audit logging.
Dedicated Cloud SQL IAM database role 'ticket_rw' is held exclusively by sa-ticket-mcp@.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ticket_router.mcp.ticket_db.schemas import (
    AppendCustomerFollowupRequest,
    AppendCustomerFollowupResponse,
    ClaimTicketRequest,
    ClaimTicketResponse,
    CustomerTicketSummary,
    GetActiveTicketRequest,
    GetActiveTicketResponse,
    GetTicketRequest,
    GetTicketResponse,
    ListCustomerTicketsRequest,
    ListCustomerTicketsResponse,
    RecordConfirmationRequestedRequest,
    RecordConfirmationRequestedResponse,
    ReidentifyPiiRequest,
    ReidentifyPiiResponse,
    ResolveCustomerRequest,
    ResolveCustomerResponse,
    ResolveTicketRequest,
    ResolveTicketResponse,
    SaveRedactionMapRequest,
    SaveRedactionMapResponse,
    UpdateTicketStatusRequest,
    UpdateTicketStatusResponse,
    UpsertTicketRequest,
    UpsertTicketResponse,
    get_tool_schemas,
)
from ticket_router.store.ids import canonical_ticket_uuid
from ticket_router.store.interfaces import CustomerRepository, TicketRepository
from ticket_router.store.models import RedactionMap, TicketCreate

logger = logging.getLogger("ticket_router.mcp.ticket_db")

router = APIRouter()


def _extract_caller_identity(
    authorization: Optional[str] = None,
    serverless_authorization: Optional[str] = None,
) -> str:
    """Extract caller identity (email or subject) from Google-signed ID token without re-verifying signature.

    Cloud Run ingress proxy verifies the JWT signature before request reaches the container.
    """
    token_header = serverless_authorization or authorization
    if not token_header or not token_header.startswith("Bearer "):
        return "sa-ticket-mcp-internal"

    token = token_header[7:].strip()
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            claims = json.loads(payload_json)
            return claims.get("email") or claims.get("sub") or "sa-authenticated-caller"
        except Exception:
            return "sa-authenticated-caller"
    return "sa-authenticated-caller"


def get_ticket_repo(request: Request) -> TicketRepository:
    """Dependency retrieving configured TicketRepository from app state."""
    try:
        return request.app.state.ticket_repo
    except AttributeError as err:
        raise RuntimeError("TicketRepository not configured on app.state") from err


def get_customer_repo(request: Request) -> CustomerRepository:
    """Dependency retrieving configured CustomerRepository from app state."""
    try:
        return request.app.state.customer_repo
    except AttributeError as err:
        raise RuntimeError("CustomerRepository not configured on app.state") from err


@router.get("/health")
@router.get("/healthz")
def healthz() -> Dict[str, str]:
    """Liveness and readiness probe conforming to Cloud Run and Kubernetes conventions."""
    return {"status": "ok"}


@router.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "ticket-db-mcp",
        "status": "running",
        "tools": list(get_tool_schemas().keys()),
    }


@router.get("/tools")
def list_tools() -> Dict[str, Any]:
    """Return formal MCP tool schemas (REQ-AGENT-010)."""
    return {"tools": get_tool_schemas()}


@router.post("/tools/upsert_ticket", response_model=UpsertTicketResponse)
@router.post("/upsert_ticket", response_model=UpsertTicketResponse)
async def upsert_ticket(
    req: UpsertTicketRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> UpsertTicketResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("upsert_ticket called by caller=%s key=%s", caller, req.idempotency_key)

    # Derive deterministic UUID from idempotency_key
    det_ticket_uuid = UUID(canonical_ticket_uuid(req.idempotency_key))

    # Check if ticket already exists (Idempotency invariant)
    existing = ticket_repo.get_ticket(det_ticket_uuid)
    if existing is not None:
        dec = req.decision or {}
        raw_queue = dec.get("queue")
        assigned_queue = None if raw_queue in (None, "", "none") else raw_queue
        # If escalating an existing ticket, update status and queue
        if dec.get("decision") == "escalate":
            if hasattr(ticket_repo, "update_ticket_status"):
                ticket_repo.update_ticket_status(
                    existing.ticket_id,
                    status="open",
                    resolution_notes=f"Escalated by {dec.get('escalated_by', 'router')}: {dec.get('reason', 'escalate')}",
                )
            if hasattr(ticket_repo, "append_event"):
                ticket_repo.append_event(
                    existing.ticket_id,
                    event_type="escalated",
                    actor=str(dec.get("escalated_by", "router")),
                    payload={"queue": assigned_queue, "reason": dec.get("reason")},
                )
        return UpsertTicketResponse(
            ticket_id=str(existing.ticket_id),
            status="open" if dec.get("decision") == "escalate" else existing.status,
            deduplicated=True,
        )

    # Map decision to initial status & queue
    dec = req.decision or {}
    raw_queue = dec.get("queue")
    assigned_queue = None if raw_queue in (None, "", "none") else raw_queue
    status_val = "open"
    details = dec.get("details") if isinstance(dec.get("details"), dict) else {}
    if (
        dec.get("decision") in ("resolve", "reject_oos")
        or dec.get("route") == "resolved"
        or details.get("category") in ("greeting", "pleasantry")
    ):
        status_val = "resolved_auto"

    from ticket_router.store.models import effective_category

    category = (
        details.get("category")
        or effective_category(decision=dec)
        or "support"
    )
    raw_q = (assigned_queue or "").strip().lower()
    if raw_q.startswith("human"):
        current_owner = f"queue:{assigned_queue}"
    else:
        current_owner = "bot"

    issue_reference = (
        dec.get("ticket_id")
        or f"TICK-{str(det_ticket_uuid)[:8]}"
    )
    session_id = dec.get("session_id")

    ticket_create = TicketCreate(
        ticket_id=det_ticket_uuid,
        customer_id=req.customer_id,
        channel=req.channel,
        body_redacted=req.body_redacted,
        decision=req.decision,
        assigned_queue=assigned_queue,
        session_id=session_id,
        issue_reference=issue_reference,
        category=category,
        current_owner=current_owner,
    )

    ticket = ticket_repo.create_ticket(ticket_create)
    if status_val != "open" and ticket.status != status_val:
        updated = ticket_repo.update_ticket_status(ticket.ticket_id, status=status_val)
        if updated is not None:
            ticket = updated

    return UpsertTicketResponse(
        ticket_id=str(ticket.ticket_id),
        status=ticket.status,
        deduplicated=False,
    )


@router.post("/tools/save_redaction_map", response_model=SaveRedactionMapResponse)
@router.post("/save_redaction_map", response_model=SaveRedactionMapResponse)
async def save_redaction_map(
    req: SaveRedactionMapRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> SaveRedactionMapResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("save_redaction_map called by caller=%s ticket=%s customer=%s", caller, req.ticket_id, req.customer_id)

    try:
        target_uuid = UUID(req.ticket_id)
    except (ValueError, AttributeError, TypeError):
        target_uuid = UUID(canonical_ticket_uuid(req.ticket_id))

    redaction_map = RedactionMap(
        map_id=uuid.uuid4(),
        ticket_id=target_uuid,
        customer_id=req.customer_id,
        encrypted_mappings=req.envelope_b64.encode("utf-8"),
        key_id=req.key_id,
    )
    try:
        ticket_repo.save_redaction_map(redaction_map)
    except Exception as exc:
        err_msg = str(exc).lower()
        if (
            "foreign key" in err_msg
            or "23503" in err_msg
            or "not found" in err_msg
            or isinstance(exc, KeyError)
        ):
            raise HTTPException(status_code=404, detail="ticket_not_found") from exc
        raise
    return SaveRedactionMapResponse(saved=True)


@router.post("/tools/claim_ticket", response_model=ClaimTicketResponse)
@router.post("/claim_ticket", response_model=ClaimTicketResponse)
async def claim_ticket(
    req: ClaimTicketRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> ClaimTicketResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("claim_ticket called by caller=%s ticket=%s agent=%s", caller, req.ticket_id, req.agent_id)

    try:
        target_uuid = UUID(req.ticket_id)
    except (ValueError, AttributeError):
        target_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{req.ticket_id}")

    ticket = ticket_repo.get_ticket(target_uuid)
    if ticket is None:
        return ClaimTicketResponse(success=False, error="not_found")

    if ticket.assigned_to and ticket.assigned_to != req.agent_id:
        return ClaimTicketResponse(success=False, error="already_claimed")

    if ticket.status != "open" and ticket.assigned_to != req.agent_id:
        return ClaimTicketResponse(success=False, error="already_claimed")

    claimed = ticket_repo.claim_ticket(target_uuid, req.agent_id)
    if not claimed and ticket.assigned_to != req.agent_id:
        return ClaimTicketResponse(success=False, error="already_claimed")

    return ClaimTicketResponse(success=True)


@router.post("/tools/resolve_ticket", response_model=ResolveTicketResponse)
@router.post("/resolve_ticket", response_model=ResolveTicketResponse)
async def resolve_ticket(
    req: ResolveTicketRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> ResolveTicketResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("resolve_ticket called by caller=%s ticket=%s", caller, req.ticket_id)

    try:
        target_uuid = UUID(req.ticket_id)
    except (ValueError, AttributeError):
        target_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{req.ticket_id}")

    ticket = ticket_repo.get_ticket(target_uuid)
    if ticket is None:
        return ResolveTicketResponse(status="not_found")

    ticket_repo.update_ticket_status(
        ticket_id=target_uuid,
        status="resolved",
        resolution_notes=req.resolution_notes,
    )
    ticket_repo.append_event(
        ticket_id=target_uuid,
        event_type="resolved",
        actor=req.agent_id,
        payload={"resolution_notes": req.resolution_notes},
    )
    return ResolveTicketResponse(status="RESOLVED")


@router.post("/tools/reidentify_pii", response_model=ReidentifyPiiResponse)
@router.post("/reidentify_pii", response_model=ReidentifyPiiResponse)
async def reidentify_pii(
    req: ReidentifyPiiRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> ReidentifyPiiResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.warning(
        "reidentify_pii denied on ticket-db-mcp caller=%s ticket=%s: role ticket_rw cannot access redaction_maps",
        caller,
        req.ticket_id,
    )
    return ReidentifyPiiResponse(
        unmasked_fields={},
        error="forbidden_role_ticket_rw_cannot_access_redaction_maps_use_human_queue",
    )


@router.post("/tools/resolve_customer", response_model=ResolveCustomerResponse)
@router.post("/resolve_customer", response_model=ResolveCustomerResponse)
async def resolve_customer(
    req: ResolveCustomerRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    customer_repo: CustomerRepository = Depends(get_customer_repo),
) -> ResolveCustomerResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("resolve_customer called by caller=%s issuer=%s", caller, req.issuer)

    res = customer_repo.resolve(req.issuer, req.subject)

    if res.linked:
        event_type = "resolve_ok" if res.status == "active" else "resolve_suspended"
    else:
        event_type = "resolve_unlinked"

    customer_repo.append_identity_event(
        customer_id=res.customer_id,
        issuer=req.issuer,
        event_type=event_type,
        actor=caller,
        payload={"subject": req.subject},
    )

    return ResolveCustomerResponse(
        customer_id=res.customer_id,
        status=res.status,
        linked=res.linked,
    )


@router.post("/tools/get_active_ticket_for_session", response_model=GetActiveTicketResponse)
@router.post("/get_active_ticket_for_session", response_model=GetActiveTicketResponse)
async def get_active_ticket_for_session(
    req: GetActiveTicketRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> GetActiveTicketResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info(
        "get_active_ticket_for_session called by caller=%s session=%s",
        caller,
        req.session_id,
    )

    ticket = ticket_repo.get_active_ticket_for_session(
        session_id=req.session_id,
        customer_id=req.customer_id,
    )
    if ticket is None:
        return GetActiveTicketResponse(
            ticket_id=None,
            status=None,
            assigned_queue=None,
            customer_id=None,
            ticket=None,
        )

    events = []
    if hasattr(ticket_repo, "list_events"):
        try:
            events = ticket_repo.list_events(ticket.ticket_id)
        except Exception:
            events = []
    has_conf = any(getattr(ev, "event_type", None) == "resolution_confirmation_requested" for ev in events)

    ticket_dict = {
        "ticket_id": str(ticket.ticket_id),
        "customer_id": ticket.customer_id,
        "channel": ticket.channel,
        "body_redacted": ticket.body_redacted,
        "decision": ticket.decision,
        "status": ticket.status,
        "assigned_to": ticket.assigned_to,
        "assigned_queue": ticket.assigned_queue,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "session_id": getattr(ticket, "session_id", None) or req.session_id,
        "resolution_confirmation_requested": has_conf,
    }
    return GetActiveTicketResponse(
        ticket_id=str(ticket.ticket_id),
        status=ticket.status,
        assigned_queue=ticket.assigned_queue,
        customer_id=ticket.customer_id,
        ticket=ticket_dict,
    )


@router.post("/tools/append_customer_followup", response_model=AppendCustomerFollowupResponse)
@router.post("/append_customer_followup", response_model=AppendCustomerFollowupResponse)
async def append_customer_followup(
    req: AppendCustomerFollowupRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> AppendCustomerFollowupResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info(
        "append_customer_followup called by caller=%s ticket=%s",
        caller,
        req.ticket_id,
    )

    try:
        event, created = ticket_repo.append_customer_followup(
            ticket_id=req.ticket_id,
            body_redacted=req.body_redacted,
            customer_id=req.customer_id,
            traceparent=req.traceparent,
            message_hash=req.message_hash,
        )
        return AppendCustomerFollowupResponse(
            success=True,
            event_id=str(event.event_id),
            deduplicated=not created,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ticket_not_found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="customer_mismatch") from exc


@router.post("/tools/update_ticket_status", response_model=UpdateTicketStatusResponse)
@router.post("/update_ticket_status", response_model=UpdateTicketStatusResponse)
async def update_ticket_status(
    req: UpdateTicketStatusRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> UpdateTicketStatusResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info(
        "update_ticket_status called by caller=%s ticket=%s to_status=%s actor=%s",
        caller,
        req.ticket_id,
        req.to_status,
        req.actor,
    )

    header_customer = x_customer_id or x_verified_customer_id
    is_customer_transition = (
        (req.to_status in ("resolved_by_customer", "reopened"))
        or (req.customer_id is not None)
        or (header_customer is not None)
    )

    if is_customer_transition:
        if not header_customer:
            raise HTTPException(
                status_code=400,
                detail="Missing verified customer identity header (X-Customer-ID)",
            )
        if str(header_customer).startswith("anon:"):
            raise HTTPException(
                status_code=403,
                detail="Anonymous customers cannot update ticket status",
            )
        if not x_customer_verified or str(x_customer_verified).lower() != "true":
            raise HTTPException(
                status_code=403,
                detail="Unverified customer identity",
            )
        if req.customer_id is not None and req.customer_id != header_customer:
            raise HTTPException(
                status_code=400,
                detail="customer_id in request body does not match verified identity header (BOLA violation)",
            )
        if req.actor and (req.actor.startswith("rep:") or req.actor.startswith("queue:")):
            raise HTTPException(
                status_code=403,
                detail="Customer cannot impersonate representative or queue actor",
            )

    try:
        updated = ticket_repo.update_ticket_status(
            ticket_id=req.ticket_id,
            status=req.to_status,
            from_status=req.from_status,
            actor=req.actor,
            reason=req.reason,
            idempotency_key=req.idempotency_key,
            trace=req.trace,
            customer_id=header_customer or req.customer_id,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        is_idempotent = (updated.status == req.to_status) and (req.from_status != req.to_status)
        return UpdateTicketStatusResponse(
            success=True,
            ticket_id=str(updated.ticket_id),
            status=updated.status,
            idempotent=is_idempotent,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ticket_not_found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        err_msg = str(exc)
        if "Status conflict" in err_msg:
            raise HTTPException(status_code=409, detail=err_msg) from exc
        raise HTTPException(status_code=400, detail=err_msg) from exc


@router.post("/tools/record_resolution_confirmation_requested", response_model=RecordConfirmationRequestedResponse)
@router.post("/record_resolution_confirmation_requested", response_model=RecordConfirmationRequestedResponse)
async def record_resolution_confirmation_requested(
    req: RecordConfirmationRequestedRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> RecordConfirmationRequestedResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info(
        "record_resolution_confirmation_requested called by caller=%s ticket=%s",
        caller,
        req.ticket_id,
    )
    try:
        target_uuid = UUID(req.ticket_id)
    except (ValueError, AttributeError):
        target_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{req.ticket_id}")

    ticket = ticket_repo.get_ticket(target_uuid)
    if ticket is None:
        try:
            ticket = ticket_repo.get_ticket(req.ticket_id)
        except Exception:
            ticket = None
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket_not_found")

    ticket_repo.append_event(
        ticket_id=ticket.ticket_id,
        event_type="resolution_confirmation_requested",
        actor="router",
        payload={"prompt": req.prompt} if req.prompt else {},
    )
    return RecordConfirmationRequestedResponse(
        success=True,
        ticket_id=str(ticket.ticket_id),
    )


@router.post("/tools/get_ticket", response_model=GetTicketResponse)
@router.post("/get_ticket", response_model=GetTicketResponse)
async def get_ticket(
    req: GetTicketRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> GetTicketResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("get_ticket called by caller=%s ticket=%s", caller, req.ticket_id)

    try:
        target_uuid = UUID(req.ticket_id)
    except (ValueError, AttributeError):
        target_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{req.ticket_id}")

    ticket = ticket_repo.get_ticket(target_uuid)
    if ticket is None:
        try:
            ticket = ticket_repo.get_ticket(req.ticket_id)
        except Exception:
            ticket = None

    if ticket is None:
        return GetTicketResponse(ticket=None, ticket_id=None, status=None)

    events = []
    if hasattr(ticket_repo, "list_events"):
        try:
            events = ticket_repo.list_events(ticket.ticket_id)
        except Exception:
            events = []
    has_conf = any(getattr(ev, "event_type", None) == "resolution_confirmation_requested" for ev in events)

    ticket_dict = {
        "ticket_id": str(ticket.ticket_id),
        "customer_id": ticket.customer_id,
        "channel": ticket.channel,
        "body_redacted": ticket.body_redacted,
        "decision": ticket.decision,
        "status": ticket.status,
        "assigned_to": ticket.assigned_to,
        "assigned_queue": ticket.assigned_queue,
        "category": getattr(ticket, "category", "support"),
        "current_owner": getattr(ticket, "current_owner", "bot"),
        "issue_reference": getattr(ticket, "issue_reference", None),
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "session_id": getattr(ticket, "session_id", None),
        "resolution_confirmation_requested": has_conf,
    }
    return GetTicketResponse(
        ticket=ticket_dict,
        ticket_id=str(ticket.ticket_id),
        status=ticket.status,
    )


@router.post("/tools/list_customer_tickets", response_model=ListCustomerTicketsResponse)
@router.post("/list_customer_tickets", response_model=ListCustomerTicketsResponse)
async def list_customer_tickets(
    req: ListCustomerTicketsRequest,
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
    ticket_repo: TicketRepository = Depends(get_ticket_repo),
) -> ListCustomerTicketsResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    logger.info("list_customer_tickets called by caller=%s customer=%s", caller, req.customer_id)

    header_customer = x_customer_id or x_verified_customer_id
    if not header_customer:
        raise HTTPException(
            status_code=400,
            detail="Missing verified customer identity header (X-Customer-ID)",
        )
    if str(header_customer).startswith("anon:"):
        raise HTTPException(
            status_code=403,
            detail="Anonymous customers cannot list tickets",
        )
    if not x_customer_verified or str(x_customer_verified).lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Unverified customer identity",
        )
    if req.customer_id is not None and req.customer_id != header_customer:
        raise HTTPException(
            status_code=400,
            detail="customer_id in request body does not match verified identity header (BOLA violation)",
        )

    try:
        tickets = ticket_repo.list_customer_tickets(
            customer_id=header_customer,
            limit=req.limit,
        )
        summaries: list[CustomerTicketSummary] = []
        for t in tickets:
            ref = t.issue_reference or f"TICK-{str(t.ticket_id)[:8]}"
            cat = getattr(t, "category", "support") or "support"
            if cat in ("security", "fraud"):
                status_str = "under review"
            else:
                status_str = t.status
            created_str = (
                t.created_at.isoformat()
                if hasattr(t.created_at, "isoformat")
                else str(t.created_at)
            )
            summaries.append(
                CustomerTicketSummary(
                    issue_reference=ref,
                    status=status_str,
                    created_at=created_str,
                    category=cat,
                )
            )
        return ListCustomerTicketsResponse(tickets=summaries)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(
    repo: Optional[TicketRepository] = None,
    redaction_store: Optional[Dict[str, Dict[str, Any]]] = None,
    customer_repo: Optional[CustomerRepository] = None,
    ticket_repo: Optional[TicketRepository] = None,
) -> FastAPI:
    """Create and configure FastAPI application for ticket-db-mcp."""
    if repo is None and ticket_repo is not None:
        repo = ticket_repo
    if repo is None:
        allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")
        if allow_fake:
            from ticket_router.store.fake import FakeCustomerRepo, FakeTicketRepo

            repo = FakeTicketRepo()
            if customer_repo is None:
                customer_repo = FakeCustomerRepo()
        else:
            raise RuntimeError(
                "TicketRepository is required to create ticket-db-mcp app (or set ALLOW_FAKE_REPOS=1)"
            )
    elif customer_repo is None:
        allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")
        if allow_fake or (repo is not None and repo.__class__.__name__ == "FakeTicketRepo"):
            from ticket_router.store.fake import FakeCustomerRepo

            customer_repo = FakeCustomerRepo()

    application = FastAPI(
        title="ticket-db-mcp",
        version="0.1.0",
        description="MCP server for ticket persistence, lifecycle events, and audited PII re-identification.",
    )

    @application.exception_handler(TimeoutError)
    async def timeout_error_handler(request: Request, exc: TimeoutError) -> JSONResponse:
        logger.error("Database connection timeout: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "database_timeout", "error": str(exc)},
        )

    application.state.ticket_repo = repo
    application.state.customer_repo = customer_repo
    application.state.pii_store = redaction_store if redaction_store is not None else {}
    application.include_router(router)
    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
