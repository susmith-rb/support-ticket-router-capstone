"""FastAPI server for human-queue agent service on Cloud Run.

# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.store.interfaces import TicketRepository

logger = logging.getLogger("ticket_router.agents.human_queue.server")

router = APIRouter()


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
    except AttributeError:
        raise RuntimeError("HumanQueueAgent not configured on app.state")


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
        raise HTTPException(status_code=403, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def create_app(
    ticket_repo: Optional[TicketRepository] = None,
    redaction_store: Optional[Dict[str, Dict[str, Any]]] = None,
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
    application.include_router(router)
    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
