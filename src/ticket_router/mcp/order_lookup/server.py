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
    GetOrderToolRequest,
    ListOrdersHttpRequest,
    ListOrdersResponse,
    ListOrdersToolRequest,
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
    except AttributeError as err:
        raise RuntimeError("OrderRepository not configured on app.state") from err


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


def _order_to_response(order: Order) -> GetOrderResponse:
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


class OrderLookupMcpServer:
    """Core domain logic for order-lookup-mcp tools (REQ-AGENT-010)."""

    def __init__(self, repo: OrderRepository) -> None:
        self.repo = repo

    def get_order_tool(self, customer_id: str, req: GetOrderToolRequest) -> GetOrderResponse:
        if not customer_id or customer_id.startswith("anon:"):
            raise PermissionError("Anonymous or unverified customer cannot access order lookup.")
        order = self.repo.get_order(order_id=req.order_id, customer_id=customer_id)
        if order is None:
            raise LookupError(f"Order '{req.order_id}' not found for customer.")
        return _order_to_response(order)

    def list_orders_tool(self, customer_id: str, req: ListOrdersToolRequest) -> ListOrdersResponse:
        if not customer_id or customer_id.startswith("anon:"):
            raise PermissionError("Anonymous or unverified customer cannot access order lookup.")
        orders = self.repo.list_customer_orders(customer_id=customer_id, limit=req.limit)
        return ListOrdersResponse(orders=[_order_to_response(o) for o in orders])


def _validate_verified_customer_headers(
    request: Request,
    x_customer_id: Optional[str],
    x_verified_customer_id: Optional[str],
    x_customer_verified: Optional[str],
    caller: str,
) -> str:
    verified_customer = x_customer_id or x_verified_customer_id
    if not verified_customer:
        raise HTTPException(
            status_code=400,
            detail="Missing verified customer context header (X-Customer-ID / X-Verified-Customer-Id).",
        )
    if verified_customer.startswith("anon:"):
        logger.warning(
            "Rejected unverified anonymous customer lookup: customer_id=%s caller=%s",
            verified_customer,
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    is_verified = (x_customer_verified or "").strip().lower() == "true"
    if x_customer_verified is not None and not is_verified:
        logger.warning(
            "Rejected unverified customer (X-Customer-Verified=%s) customer_id=%s caller=%s",
            x_customer_verified,
            verified_customer,
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    require_verified_header = bool(getattr(request.app.state, "require_verified_header", False))
    if require_verified_header and not is_verified:
        logger.warning(
            "Rejected customer lookup without required X-Customer-Verified: true header customer_id=%s caller=%s",
            verified_customer,
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    return verified_customer


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
    return _order_to_response(order)


@router.post("/tools/get_order", response_model=GetOrderResponse)
async def get_order_tool(
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
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
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

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

    # Customer Identity Guard (REQ-IDENT-004, REQ-IDENT-005):
    if verified_customer.startswith("anon:"):
        logger.warning("Rejected unverified anonymous customer lookup: customer_id=%s caller=%s", verified_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )

    is_verified = (x_customer_verified or "").strip().lower() == "true"
    if x_customer_verified is not None and not is_verified:
        logger.warning("Rejected unverified customer (X-Customer-Verified=%s) customer_id=%s caller=%s", x_customer_verified, verified_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )
    require_verified_header = bool(request.app.state.require_verified_header)
    if require_verified_header and not is_verified:
        logger.warning("Rejected customer lookup without required X-Customer-Verified: true header customer_id=%s caller=%s", verified_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )

    order_id = body_dict.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Field 'order_id' is required.")

    return _lookup_order_for_customer(repo, str(order_id), verified_customer)


@router.post("/get_order", response_model=GetOrderResponse)
async def get_order_http(
    req: GetOrderHttpRequest,
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
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

    # Customer Identity Guard (REQ-IDENT-004, REQ-IDENT-005):
    if effective_customer.startswith("anon:"):
        logger.warning("Rejected unverified anonymous customer lookup: customer_id=%s caller=%s", effective_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )

    is_verified = (x_customer_verified or "").strip().lower() == "true"
    if x_customer_verified is not None and not is_verified:
        logger.warning("Rejected unverified customer (X-Customer-Verified=%s) customer_id=%s caller=%s", x_customer_verified, effective_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )
    require_verified_header = bool(request.app.state.require_verified_header)
    if require_verified_header and not is_verified:
        logger.warning("Rejected customer lookup without required X-Customer-Verified: true header customer_id=%s caller=%s", effective_customer, caller)
        raise HTTPException(
            status_code=403,
            detail="unverified_customer",
        )

    return _lookup_order_for_customer(repo, req.order_id, effective_customer)


@router.post("/tools/list_orders", response_model=ListOrdersResponse)
async def list_orders_tool(
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> ListOrdersResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    verified_customer = _validate_verified_customer_headers(
        request, x_customer_id, x_verified_customer_id, x_customer_verified, caller
    )

    body_bytes = await request.body()
    try:
        body_dict = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if "customer_id" in body_dict:
        logger.warning(
            "BOLA guard: rejected untrusted customer_id in tool arguments from caller=%s",
            caller,
        )
        raise HTTPException(
            status_code=400,
            detail="BOLA/IDOR violation: customer_id is strictly derived from verified session claims and must not be passed as a tool parameter (REQ-AGENT-010, AGENTS.md rule 7).",
        )

    try:
        tool_req = ListOrdersToolRequest.model_validate(body_dict)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Validation error: {exc}") from exc

    server = OrderLookupMcpServer(repo)
    try:
        return server.list_orders_tool(verified_customer, tool_req)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/list_orders", response_model=ListOrdersResponse)
async def list_orders_http(
    req: ListOrdersHttpRequest,
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> ListOrdersResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)
    verified_customer = _validate_verified_customer_headers(
        request, x_customer_id, x_verified_customer_id, x_customer_verified, caller
    )
    server = OrderLookupMcpServer(repo)
    tool_req = ListOrdersToolRequest(limit=req.limit)
    try:
        return server.list_orders_tool(verified_customer, tool_req)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def create_app(
    order_repo: Optional[OrderRepository] = None,
    require_verified_header: bool = False,
) -> FastAPI:
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
    application.state.require_verified_header = require_verified_header
    application.include_router(router)
    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
