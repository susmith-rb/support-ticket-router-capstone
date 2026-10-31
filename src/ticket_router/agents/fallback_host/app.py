"""FastAPI serving application for Cloud Run fallback agent host (triage-fallback).

# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)
# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-017: Agent Runtime & Vertex AI Agent Engine Deployment
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-007: Defense-in-Depth Against Caller-Asserted Identity

Exposes the TriageRouter agent over HTTP using FastAPI, reusing the shared
TriageRouterAdkAgent factory from ticket_router.agents.triage_router.adk_agent
without any duplicated routing, classification, or policy logic.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from ticket_router.agents.fallback_host.sessions import (
    MEMORY_BANK_DEGRADATION_REASON,
    SESSION_DEGRADATION_REASON,
    InMemorySessionStore,
)
from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    root_agent,
)
from ticket_router.common.settings import Settings

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, status
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

logger = logging.getLogger("ticket_router.agents.fallback_host.app")


def verify_caller_auth_headers(
    headers: Dict[str, str],
    settings: Settings,
    audience: Optional[str] = None,
    token_verifier: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Verify Google OIDC ID token from inbound caller headers.

    Enforces REQ-AGENT-035 caller authentication contract:
    - In production (require_real_deps=True or REQUIRE_CALLER_AUTH=1 or ALLOWED_CALLERS configured):
      Requires valid Google-signed OIDC ID token with verified email matching allowed callers.
    - Never accepts magic test-token prefixes unless an explicit token_verifier is injected by a unit test.
    """
    require_auth = (
        os.getenv("REQUIRE_CALLER_AUTH", "").lower() in ("1", "true")
        or settings.require_real_deps
        or bool(settings.allowed_callers)
    )

    auth_header = headers.get("authorization", "")
    if not auth_header:
        if not require_auth:
            return {"email": "dev-caller@example.com", "email_verified": True}
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header. Expected 'Bearer <token>'.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise PermissionError("Missing Authorization header")

    if not auth_header.startswith("Bearer "):
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise PermissionError("Invalid Authorization header format")

    token = auth_header[7:].strip()
    if not token:
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty Bearer token provided in Authorization header.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise PermissionError("Empty Bearer token provided")

    expected_audience = audience or os.getenv("FALLBACK_AUDIENCE") or os.getenv("TRIAGE_FALLBACK_URL")

    if token_verifier is not None:
        try:
            claims = token_verifier(token, expected_audience)
        except Exception as exc:
            logger.warning("Injected caller token verification failed: %s", exc)
            if _FASTAPI_AVAILABLE:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid Google ID token: {exc}",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            raise PermissionError(f"Invalid Google ID token: {exc}") from exc
    else:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token as google_id_token

        try:
            claims = google_id_token.verify_oauth2_token(
                token,
                GoogleAuthRequest(),
                audience=expected_audience,
            )
        except Exception as exc:
            logger.warning("Google ID token verification failed: %s", exc)
            if _FASTAPI_AVAILABLE:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid Google ID token: {exc}",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            raise PermissionError(f"Invalid Google ID token: {exc}") from exc

    email = str(claims.get("email", ""))
    email_verified = bool(claims.get("email_verified", False))
    if not email_verified:
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Caller ID token email is not verified by issuer.",
            )
        raise PermissionError("Caller ID token email is not verified")

    if settings.allowed_callers and email not in settings.allowed_callers:
        logger.warning("Caller '%s' denied: not in ALLOWED_CALLERS (%s)", email, settings.allowed_callers)
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Caller '{email}' is not in ALLOWED_CALLERS allowlist.",
            )
        raise PermissionError(f"Caller '{email}' is not in ALLOWED_CALLERS")

    return claims


def verify_caller_auth(request: Request) -> Dict[str, Any]:
    """FastAPI dependency to verify inbound caller authentication."""
    try:
        settings = request.app.state.settings
    except AttributeError:
        settings = Settings.from_env()

    headers_map = {k.lower(): v for k, v in request.headers.items()}
    audience = getattr(request.app.state, "audience", None)
    token_verifier = getattr(request.app.state, "token_verifier", None)
    return verify_caller_auth_headers(
        headers_map,
        settings,
        audience=audience,
        token_verifier=token_verifier,
    )


def create_app(
    agent: Optional[TriageRouterAdkAgent] = None,
    session_store: Optional[InMemorySessionStore] = None,
    settings: Optional[Settings] = None,
    audience: Optional[str] = None,
    token_verifier: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
) -> FastAPI:
    """Create and configure FastAPI application for Cloud Run fallback agent host."""
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("FastAPI is required to instantiate triage_fallback app")

    app = FastAPI(
        title="TriageRouter Fallback Host",
        description=(
            "Cloud Run warm fallback host for TriageRouterAgent when Vertex AI Agent Engine "
            "is unavailable. Reuses the canonical TriageRouterAgent factory and ADK wrapper."
        ),
        version="1.0.0",
    )

    resolved_settings = settings or Settings.from_env()
    resolved_session_store = session_store or InMemorySessionStore()
    resolved_agent = agent or root_agent

    app.state.settings = resolved_settings
    app.state.session_store = resolved_session_store
    app.state.agent = resolved_agent
    app.state.audience = audience or os.getenv("FALLBACK_AUDIENCE") or os.getenv("TRIAGE_FALLBACK_URL")
    app.state.token_verifier = token_verifier

    # -------------------------------------------------------------------------
    # Health and Metadata Endpoints (Unauthenticated for Probes)
    # -------------------------------------------------------------------------
    @app.get("/health")
    @app.get("/healthz")
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint for Cloud Run and LB probes.

        Explicitly documents degraded mode for Sessions and Memory Bank.
        """
        return {
            "status": "healthy",
            "service": "triage-fallback",
            "runtime": "cloud-run",
            "agent": "TriageRouterAgent",
            "degraded_mode": {
                "sessions": "degraded_in_memory",
                "sessions_detail": SESSION_DEGRADATION_REASON,
                "memory_bank": "unavailable",
                "memory_bank_detail": MEMORY_BANK_DEGRADATION_REASON,
            },
        }

    # -------------------------------------------------------------------------
    # Session Management Endpoints (Cloud SQL-Free, In-Memory)
    # -------------------------------------------------------------------------
    @app.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session_endpoint(
        request: Request,
        caller: Dict[str, Any] = Depends(verify_caller_auth),
    ) -> Dict[str, Any]:
        """Create or initialize an in-memory session (Cloud SQL-free)."""
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        session_id = body.get("session_id") or body.get("id")
        user_id = body.get("user_id") or caller.get("email") or "anon"
        state = body.get("state") or {}

        store: InMemorySessionStore = request.app.state.session_store
        session = store.create_session(session_id=session_id, user_id=user_id, state=state)
        return {
            "id": session["id"],
            "session_id": session["session_id"],
            "user_id": session["user_id"],
            "state": session["state"],
            "degraded": True,
            "degraded_notice": store.degradation_notice,
        }

    @app.get("/sessions/{session_id}")
    async def get_session_endpoint(
        session_id: str,
        request: Request,
        caller: Dict[str, Any] = Depends(verify_caller_auth),
    ) -> Dict[str, Any]:
        """Retrieve existing in-memory session."""
        store: InMemorySessionStore = request.app.state.session_store
        session = store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return session

    @app.delete("/sessions/{session_id}")
    async def delete_session_endpoint(
        session_id: str,
        request: Request,
        caller: Dict[str, Any] = Depends(verify_caller_auth),
    ) -> Dict[str, Any]:
        """Delete an in-memory session."""
        store: InMemorySessionStore = request.app.state.session_store
        deleted = store.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        return {"status": "deleted", "session_id": session_id}

    # -------------------------------------------------------------------------
    # Triage Query Endpoints (Authenticated)
    # -------------------------------------------------------------------------
    async def _handle_query(
        request: Request,
        caller: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle synchronous triage query via shared TriageRouterAgent factory."""
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        message = body.get("message") or body.get("text") or body.get("query") or body.get("user_content") or ""
        if not str(message).strip():
            raise HTTPException(status_code=400, detail="Missing required 'message' in request body")

        session_id = body.get("session_id") or "default_session"
        user_id = body.get("user_id") or caller.get("email") or "anon"

        # Customer token resolution (REQ-IDENT-003, REQ-IDENT-007):
        # 1. Directly in request body
        # 2. X-Customer-Id-Token header
        # 3. Existing in-memory session state
        store: InMemorySessionStore = request.app.state.session_store
        existing_session = store.get_session(session_id)
        session_state = existing_session.get("state", {}) if existing_session else {}

        customer_id_token = (
            body.get("customer_id_token")
            or request.headers.get("x-customer-id-token")
            or session_state.get("customer_id_token")
        )
        oauth_access_token = (
            body.get("oauth_access_token")
            or request.headers.get("x-oauth-access-token")
            or session_state.get("oauth_access_token")
        )

        # Update session state with supplied tokens
        state_updates: Dict[str, Any] = {}
        if customer_id_token:
            state_updates["customer_id_token"] = customer_id_token
        if oauth_access_token:
            state_updates["oauth_access_token"] = oauth_access_token
        if state_updates:
            store.get_or_create_session(session_id=session_id, user_id=user_id)
            store.update_session_state(session_id, state_updates)

        adk_agent_instance: TriageRouterAdkAgent = request.app.state.agent

        # Delegate execution in thread pool to prevent blocking FastAPI event loop
        def execute_agent_query() -> Dict[str, Any]:
            return adk_agent_instance.query(
                message=str(message),
                session_id=str(session_id),
                customer_id_token=customer_id_token,
                oauth_access_token=oauth_access_token,
            )

        res = await run_in_threadpool(execute_agent_query)

        # Structure response with both 'answer' and 'reply' fields for client compatibility
        answer_text = res.get("answer") or res.get("calming_ack") or "Your request has been processed."
        status_code = res.get("status_code", 200)

        response_payload = {
            "ticket_id": res.get("ticket_id"),
            "session_id": res.get("session_id", session_id),
            "decision": res.get("decision"),
            "queue": res.get("queue"),
            "intent": res.get("intent"),
            "calming_ack": res.get("calming_ack"),
            "answer": answer_text,
            "reply": answer_text,
            "latency_ms": res.get("latency_ms", 0.0),
            "status": "COMPLETED",
            "runtime": "cloud-run-fallback",
            "degraded_mode": store.degradation_notice,
        }
        if status_code != 200:
            response_payload["status_code"] = status_code
        if res.get("error"):
            response_payload["error"] = res["error"]

        if status_code != 200:
            return JSONResponse(status_code=status_code, content=response_payload)

        return response_payload

    @app.post("/")
    @app.post("/query")
    @app.post("/triage")
    async def query_endpoint(
        request: Request,
        caller: Dict[str, Any] = Depends(verify_caller_auth),
    ) -> Dict[str, Any]:
        """Process support inquiry on Cloud Run fallback host."""
        return await _handle_query(request, caller)

    return app
