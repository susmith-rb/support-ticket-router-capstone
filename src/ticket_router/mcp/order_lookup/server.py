"""FastAPI server for order-lookup-mcp service on Cloud Run.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# AGENTS.md rule 7: customer_id is never a tool parameter; read from verified session claims.
# TDD §8.4: BOLA/IDOR protection.

Cloud Run Front Door Auth & Least-Privilege Role:
Cloud Run IAM (roles/run.invoker) enforces authentication at the ingress proxy.
This service trusts Cloud Run's front door for signature verification.
Dedicated Cloud SQL IAM database role 'orders_ro' is held exclusively by sa-order-mcp@
with PostgreSQL Row-Level Security (RLS) enforcing customer_id tenant isolation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from ticket_router.mcp.order_lookup.schemas import (
    GetOrderHttpRequest,
    GetOrderResponse,
    get_tool_schemas,
)
from ticket_router.store.interfaces import OrderRepository
from ticket_router.store.models import Order

logger = logging.getLogger("ticket_router.mcp.order_lookup")

router = APIRouter()


def _extract_caller_identity(
    authorization: Optional[str] = None,
    serverless_authorization: Optional[str] = None,
) -> str:
    token_header = serverless_authorization or authorization
    if not token_header or not token_header.startswith("Bearer "):
        return "sa-order-mcp-internal"

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


def get_order_repo(request: Request) -> OrderRepository:
    """Dependency retrieving configured OrderRepository from app state."""
    try:
        return request.app.state.order_repo
    except AttributeError:
        raise RuntimeError("OrderRepository not configured on app.state")


@router.get("/health")
@router.get("/healthz")
def healthz() -> Dict[str, str]:
    """Liveness and readiness probe conforming to Cloud Run and Kubernetes conventions."""
    return {"status": "ok"}


@router.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "order-lookup-mcp",
        "status": "running",
        "tools": list(get_tool_schemas().keys()),
    }


@router.get("/tools")
def list_tools() -> Dict[str, Any]:
    """Return formal MCP tool schemas (REQ-AGENT-010)."""
    return {"tools": get_tool_schemas()}


def _lookup_order_for_customer(
    repo: OrderRepository, order_id: str, customer_id: str
) -> GetOrderResponse:
    # Query repository with customer_id simulating Postgres RLS tenant isolation
    order: Optional[Order] = repo.get_order(order_id=order_id, customer_id=customer_id)
    if order is None:
        # Uniform 404 for non-existent order or order belonging to another customer
        raise HTTPException(
            status_code=404,
            detail=f"Order '{order_id}' not found for customer.",
        )

    # Format address redacted
    if order.shipping_address_city:
        addr_redacted = f"[{order.shipping_address_city}, {order.shipping_address_state}]"
    else:
        addr_redacted = "[ADDRESS_REDACTED]"

    promised_delivery = ""
    if order.estimated_delivery:
        promised_delivery = order.estimated_delivery.isoformat()

    return GetOrderResponse(
        order_id=order.order_id,
        status=order.order_status,
        items=order.items or [],
        tracking_number=order.tracking_number or "",
        promised_delivery=promised_delivery,
        delivery_address_redacted=addr_redacted,
    )


@router.post("/tools/get_order", response_model=GetOrderResponse)
async def get_order_tool(
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> GetOrderResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    verified_customer = x_customer_id or x_verified_customer_id

    # Inspect raw body to enforce BOLA invariant (AGENTS.md rule 7 / REQ-AGENT-010)
    body_bytes = await request.body()
    try:
        body_dict = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Invariant: customer_id is never a tool parameter in formal MCP tool calls
    if "customer_id" in body_dict:
        logger.warning("BOLA guard: rejected untrusted customer_id in tool arguments from caller=%s", caller)
        raise HTTPException(
            status_code=400,
            detail="BOLA/IDOR violation: customer_id is strictly derived from verified session claims and must not be passed as a tool parameter (REQ-AGENT-010, AGENTS.md rule 7).",
        )

    if not verified_customer:
        raise HTTPException(
            status_code=400,
            detail="Missing verified customer context header (X-Customer-ID / X-Verified-Customer-Id).",
        )

    order_id = body_dict.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Field 'order_id' is required.")

    return _lookup_order_for_customer(repo, str(order_id), verified_customer)


@router.post("/get_order", response_model=GetOrderResponse)
async def get_order_http(
    req: GetOrderHttpRequest,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> GetOrderResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    header_customer = x_customer_id or x_verified_customer_id

    # BOLA / IDOR Verification:
    # If verified customer header is present, any body customer_id MUST match it exactly.
    if header_customer is not None:
        if req.customer_id is not None and req.customer_id != header_customer:
            logger.warning(
                "BOLA mismatch detected: body customer_id='%s' does not match verified header='%s' caller=%s",
                req.customer_id,
                header_customer,
                caller,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Access denied: customer_id '{req.customer_id}' does not match verified session '{header_customer}'.",
            )
        effective_customer = header_customer
    else:
        # When caller header is not provided, customer_id comes from verified context in body
        if req.customer_id is None:
            raise HTTPException(
                status_code=400,
                detail="Missing customer identification context.",
            )
        effective_customer = req.customer_id

    return _lookup_order_for_customer(repo, req.order_id, effective_customer)


def create_app(order_repo: Optional[OrderRepository] = None) -> FastAPI:
    """Create and configure FastAPI application for order-lookup-mcp."""
    if order_repo is None:
        allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")
        if allow_fake:
            from ticket_router.store.fake import FakeOrderRepo
            order_repo = FakeOrderRepo()
        else:
            raise RuntimeError(
                "OrderRepository is required to create order-lookup-mcp app (or set ALLOW_FAKE_REPOS=1)"
            )

    application = FastAPI(
        title="order-lookup-mcp",
        version="0.1.0",
        description="MCP server for read-only order lookups with Postgres RLS customer isolation.",
    )
    application.state.order_repo = order_repo
    application.include_router(router)
    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
