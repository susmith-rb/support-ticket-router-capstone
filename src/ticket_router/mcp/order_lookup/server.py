"""FastAPI server for order-lookup-mcp service on Cloud Run.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
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
import threading
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ticket_router.common.settings import Settings
from ticket_router.identity.customer_token import (
    CustomerContextError,
    KmsPublicKeyResolver,
    verify_customer_context_token,
)
from ticket_router.mcp.order_lookup.schemas import (
    GetOrderResponse,
    GetOrderToolRequest,
    ListOrdersResponse,
    ListOrdersToolRequest,
    get_tool_schemas,
)
from ticket_router.store.interfaces import OrderRepository
from ticket_router.store.models import Order

logger = logging.getLogger("ticket_router.mcp.order_lookup")

router = APIRouter()

_DEFAULT_KMS_RESOLVER: Optional[KmsPublicKeyResolver] = None
_DEFAULT_KMS_RESOLVER_LOCK = threading.Lock()


def _get_default_kms_resolver() -> KmsPublicKeyResolver:
    global _DEFAULT_KMS_RESOLVER
    if _DEFAULT_KMS_RESOLVER is None:
        with _DEFAULT_KMS_RESOLVER_LOCK:
            if _DEFAULT_KMS_RESOLVER is None:
                _DEFAULT_KMS_RESOLVER = KmsPublicKeyResolver()
    return _DEFAULT_KMS_RESOLVER


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


class CallerNotAuthorizedError(HTTPException):
    """Raised when the caller SPIFFE ID does not match ALLOWED_CALLER_SPIFFE_IDS (REQ-AUTH-009)."""

    def __init__(self, detail: str = "caller_not_authorized") -> None:
        super().__init__(status_code=403, detail=detail)


_ALLOWED_CALLERS_WARNING_LOGGED = False
_ALLOWED_CALLERS_WARNING_LOCK = threading.Lock()


def _extract_forwarded_token_sub(
    authorization: Optional[str] = None,
    serverless_authorization: Optional[str] = None,
) -> Optional[str]:
    """Extract sub claim from forwarded bearer token without signature verification.

    # REQ-AUTH-009: Cloud Run validates the bearer token at ingress and forwards it
    # with the signature replaced by SIGNATURE_REMOVED_BY_GOOGLE. We decode the
    # base64url payload to read sub. Never log the token or token payload.
    """
    token_header = serverless_authorization or authorization
    if not token_header or not token_header.startswith("Bearer "):
        return None

    token = token_header[7:].strip()
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            claims = json.loads(payload_json)
            sub = claims.get("sub")
            if sub:
                return str(sub)
        except Exception:
            return None
    return None


def _check_caller_authorization(
    request: Request,
    authorization: Optional[str] = None,
    serverless_authorization: Optional[str] = None,
) -> None:
    """Authorize caller identity before CCT verification (REQ-AUTH-009).

    Requires caller's forwarded token sub in ALLOWED_CALLER_SPIFFE_IDS.
    When empty/unset, logs a warning once and allows staging.
    Raises CallerNotAuthorizedError (HTTP 403 {"error": "caller_not_authorized"}) otherwise.
    Never logs tokens.
    """
    global _ALLOWED_CALLERS_WARNING_LOGGED
    allowed_ids = getattr(request.app.state, "allowed_caller_spiffe_ids", None)
    if allowed_ids is None:
        raw = os.environ.get("ALLOWED_CALLER_SPIFFE_IDS", "").strip()
        if raw:
            allowed_ids = [s.strip() for s in raw.split(",") if s.strip()]
        else:
            allowed_ids = []

    if not allowed_ids:
        if not _ALLOWED_CALLERS_WARNING_LOGGED:
            with _ALLOWED_CALLERS_WARNING_LOCK:
                if not _ALLOWED_CALLERS_WARNING_LOGGED:
                    logger.warning(
                        "ALLOWED_CALLER_SPIFFE_IDS is empty; allowing unvalidated caller identity for staging"
                    )
                    _ALLOWED_CALLERS_WARNING_LOGGED = True
        return

    sub = _extract_forwarded_token_sub(authorization, serverless_authorization)
    if not sub or sub not in allowed_ids:
        raise CallerNotAuthorizedError()


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
    header_customer = x_customer_id or x_verified_customer_id
    if header_customer is None:
        raise HTTPException(
            status_code=400,
            detail="Missing verified customer context header (X-Customer-ID / X-Verified-Customer-Id).",
        )
    effective_customer = header_customer

    if effective_customer.startswith("anon:"):
        logger.warning(
            "Rejected unverified anonymous customer lookup: caller=%s reason=anonymous_customer",
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    is_verified = (x_customer_verified or "").strip().lower() == "true"
    if x_customer_verified is not None and not is_verified:
        logger.warning(
            "Rejected unverified customer: caller=%s reason=customer_not_verified",
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    require_verified_header = bool(getattr(request.app.state, "require_verified_header", False))
    if require_verified_header and not is_verified:
        logger.warning(
            "Rejected customer lookup without required X-Customer-Verified: caller=%s reason=missing_verified_header",
            caller,
        )
        raise HTTPException(status_code=403, detail="unverified_customer")

    return effective_customer


async def _resolve_customer(
    request: Request,
    x_customer_context: Optional[str],
    x_customer_id: Optional[str],
    x_verified_customer_id: Optional[str],
    x_customer_verified: Optional[str],
    caller: str,
) -> str:
    mode = getattr(request.app.state, "cct_mode", None)
    if mode is None:
        settings = getattr(request.app.state, "settings", None)
        if settings is None:
            settings = Settings.from_env()
        mode = settings.cct_mode
    mode = (mode or "accept").strip().lower()

    if mode not in {"off", "accept", "require"}:
        logger.warning("Unknown cct_mode '%s', failing closed for caller=%s", mode, caller)
        raise HTTPException(status_code=403, detail="invalid_customer_context")

    if mode == "off":
        return _validate_verified_customer_headers(
            request=request,
            x_customer_id=x_customer_id,
            x_verified_customer_id=x_verified_customer_id,
            x_customer_verified=x_customer_verified,
            caller=caller,
        )

    if mode == "require" and not x_customer_context:
        logger.warning("CCT missing in require mode for caller=%s", caller)
        raise HTTPException(status_code=403, detail="invalid_customer_context")

    if x_customer_context:
        resolver = getattr(request.app.state, "cct_resolver", None)
        if resolver is None:
            resolver = _get_default_kms_resolver()

        settings = getattr(request.app.state, "settings", None)
        if settings is None:
            settings = Settings.from_env()

        audience = getattr(request.app.state, "cct_audience", None)
        if audience is None:
            audience = settings.cct_audience

        allowed_issuers = getattr(request.app.state, "cct_allowed_issuers", None)
        if allowed_issuers is None:
            allowed_issuers = settings.cct_allowed_issuers

        key_prefix = getattr(request.app.state, "cct_key_prefix", None)
        if key_prefix is None:
            key_prefix = settings.cct_key_prefix

        try:
            verified_ctx = await run_in_threadpool(
                verify_customer_context_token,
                x_customer_context,
                resolver=resolver,
                audience=audience,
                allowed_issuers=allowed_issuers,
                key_prefix=key_prefix,
            )
            logger.info("CCT verified: jti=%s", verified_ctx.jti)
        except CustomerContextError as exc:
            logger.warning("CCT verification failed: reason=%s caller=%s", exc.reason, caller)
            raise HTTPException(status_code=403, detail="invalid_customer_context") from exc

        sub = verified_ctx.customer_id
        header_customer = x_customer_id or x_verified_customer_id
        if header_customer is not None and header_customer != sub:
            logger.warning("CCT customer mismatch: jti=%s caller=%s", verified_ctx.jti, caller)
            raise HTTPException(status_code=403, detail="customer_context_mismatch")

        return sub

    return _validate_verified_customer_headers(
        request=request,
        x_customer_id=x_customer_id,
        x_verified_customer_id=x_verified_customer_id,
        x_customer_verified=x_customer_verified,
        caller=caller,
    )


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
    x_customer_context: Optional[str] = Header(None, alias="X-Customer-Context"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> GetOrderResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)

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

    # Authorize caller SPIFFE identity BEFORE CCT verification (REQ-AUTH-009)
    _check_caller_authorization(
        request=request,
        authorization=authorization,
        serverless_authorization=x_serverless_authorization,
    )

    verified_customer = await _resolve_customer(
        request=request,
        x_customer_context=x_customer_context,
        x_customer_id=x_customer_id,
        x_verified_customer_id=x_verified_customer_id,
        x_customer_verified=x_customer_verified,
        caller=caller,
    )

    order_id = body_dict.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Field 'order_id' is required.")

    return _lookup_order_for_customer(repo, str(order_id), verified_customer)


@router.post("/tools/list_orders", response_model=ListOrdersResponse)
async def list_orders_tool(
    request: Request,
    x_customer_id: Optional[str] = Header(None, alias="X-Customer-ID"),
    x_verified_customer_id: Optional[str] = Header(None, alias="X-Verified-Customer-Id"),
    x_customer_verified: Optional[str] = Header(None, alias="X-Customer-Verified"),
    x_customer_context: Optional[str] = Header(None, alias="X-Customer-Context"),
    authorization: Optional[str] = Header(None),
    x_serverless_authorization: Optional[str] = Header(None),
    repo: OrderRepository = Depends(get_order_repo),
) -> ListOrdersResponse:
    caller = _extract_caller_identity(authorization, x_serverless_authorization)

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

    # Authorize caller SPIFFE identity BEFORE CCT verification (REQ-AUTH-009)
    _check_caller_authorization(
        request=request,
        authorization=authorization,
        serverless_authorization=x_serverless_authorization,
    )

    verified_customer = await _resolve_customer(
        request=request,
        x_customer_context=x_customer_context,
        x_customer_id=x_customer_id,
        x_verified_customer_id=x_verified_customer_id,
        x_customer_verified=x_customer_verified,
        caller=caller,
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


async def _caller_not_authorized_handler(
    request: Request, exc: CallerNotAuthorizedError
) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "caller_not_authorized"})


def create_app(
    order_repo: Optional[OrderRepository] = None,
    require_verified_header: bool = False,
    cct_mode: Optional[str] = None,
    cct_resolver: Optional[Any] = None,
    cct_audience: Optional[str] = None,
    cct_allowed_issuers: Optional[Iterable[str]] = None,
    cct_key_prefix: Optional[str] = None,
    allowed_caller_spiffe_ids: Optional[Iterable[str]] = None,
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
    if allowed_caller_spiffe_ids is not None:
        application.state.allowed_caller_spiffe_ids = tuple(allowed_caller_spiffe_ids)

    application.add_exception_handler(CallerNotAuthorizedError, _caller_not_authorized_handler)

    if cct_mode is not None:
        cct_mode_norm = cct_mode.strip().lower()
        if cct_mode_norm not in {"off", "accept", "require"}:
            raise ValueError(
                f"Invalid cct_mode '{cct_mode}'; must be one of 'off', 'accept', 'require'"
            )
        application.state.cct_mode = cct_mode_norm
    if cct_resolver is not None:
        application.state.cct_resolver = cct_resolver
    if cct_audience is not None:
        application.state.cct_audience = cct_audience
    if cct_allowed_issuers is not None:
        application.state.cct_allowed_issuers = tuple(cct_allowed_issuers)
    if cct_key_prefix is not None:
        application.state.cct_key_prefix = cct_key_prefix
    application.include_router(router)
    return application


app: Optional[FastAPI] = None
if os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true"):
    app = create_app()
