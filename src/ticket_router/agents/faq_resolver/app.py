"""app.py — HTTP serving entrypoint for FaqResolverAgent on GKE Autopilot.

# REQ-AGENT-001, REQ-AGENT-006, REQ-AGENT-007.
# Exposes /health and /healthz (liveness/readiness), /card (A2A discovery), and /a2a / /resolve
# (inquiry processing). Enforces Google-signed OIDC ID token verification for inbound callers.
# Provides native FastAPI app when installed, with a zero-dependency ASGI fallback.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from ticket_router.agents.faq_resolver.a2a import (
    FAQ_RESOLVER_AGENT_CARD,
    handle_a2a_request,
)
from ticket_router.agents.faq_resolver.agent import (
    FaqResolverAgent,
    root_agent,
)
from ticket_router.common.settings import Settings

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


router = APIRouter() if _FASTAPI_AVAILABLE else None


def get_active_agent(request: Request) -> FaqResolverAgent:
    """Retrieve active agent from FastAPI application state or default to root_agent."""
    try:
        return request.app.state.agent
    except AttributeError:
        return root_agent


def verify_auth_headers(headers: Dict[str, str], settings: Settings) -> Dict[str, Any]:
    """Verify Google OIDC ID token from request headers dict."""
    auth_header = headers.get("authorization", "")
    if not auth_header:
        if not settings.require_real_deps and not settings.allowed_callers:
            return {"email": "dev@example.com", "email_verified": True}
        if _FASTAPI_AVAILABLE:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        raise PermissionError("Missing Authorization header")

    if not auth_header.startswith("Bearer "):
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
            )
        raise PermissionError("Invalid Authorization header format.")

    token = auth_header[7:].strip()
    audience = settings.faq_resolver_audience or settings.faq_resolver_url

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token as google_id_token

    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            GoogleAuthRequest(),
            audience=audience,
        )
    except Exception as exc:
        if _FASTAPI_AVAILABLE:
            raise HTTPException(status_code=401, detail=f"Invalid ID token: {exc}") from exc
        raise PermissionError(f"Invalid ID token: {exc}") from exc

    email = str(claims.get("email", ""))
    email_verified = bool(claims.get("email_verified", False))
    if not email_verified:
        if _FASTAPI_AVAILABLE:
            raise HTTPException(status_code=401, detail="ID token email is not verified")
        raise PermissionError("ID token email is not verified")

    if settings.allowed_callers and email not in settings.allowed_callers:
        if _FASTAPI_AVAILABLE:
            raise HTTPException(
                status_code=403,
                detail=f"Caller '{email}' is not in ALLOWED_CALLERS: {settings.allowed_callers}",
            )
        raise PermissionError(f"Caller '{email}' is not in ALLOWED_CALLERS")

    return claims


def verify_inbound_auth(request: Request) -> Dict[str, Any]:
    """FastAPI dependency verifying inbound Google OIDC ID token."""
    try:
        settings = request.app.state.settings
    except AttributeError:
        settings = Settings.from_env()

    headers_map = {k.lower(): v for k, v in request.headers.items()}
    return verify_auth_headers(headers_map, settings)


if _FASTAPI_AVAILABLE:
    @router.get("/health")
    @router.get("/healthz")
    async def healthz(
        active_agent: FaqResolverAgent = Depends(get_active_agent),
    ) -> Dict[str, Any]:
        return {
            "status": "healthy",
            "agent": "FaqResolverAgent",
            "model_role": active_agent.model_role,
            "resolved_model_id": active_agent.resolved_model_id,
        }

    @router.get("/card")
    async def get_agent_card() -> Dict[str, Any]:
        return FAQ_RESOLVER_AGENT_CARD

    @router.post("/a2a")
    @router.post("/resolve")
    @router.post("/a2a/handoff")
    async def resolve_endpoint(
        request: Request,
        active_agent: FaqResolverAgent = Depends(get_active_agent),
        auth_claims: Dict[str, Any] = Depends(verify_inbound_auth),
    ) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON payload: {e}"
            ) from e

        return handle_a2a_request(payload, agent=active_agent)


def create_fastapi_app(
    agent: Optional[FaqResolverAgent] = None,
    settings: Optional[Settings] = None,
) -> Any:
    """Build and return a FastAPI application for GKE Autopilot container serving."""
    resolved_settings = settings or Settings.from_env()
    resolved_agent = agent if agent is not None else root_agent

    application = FastAPI(
        title="FaqResolverAgent Service",
        version="1.0.0",
        description="GKE Autopilot microservice for bounded ReAct FAQ resolution with citations.",
    )
    application.state.settings = resolved_settings
    application.state.agent = resolved_agent
    application.include_router(router)
    return application


class AsgiFallbackApp:
    """Lightweight pure-Python ASGI application for environments without FastAPI."""

    def __init__(
        self,
        agent: Optional[FaqResolverAgent] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.agent = agent if agent is not None else root_agent

    async def handle_request(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        if path in ("/health", "/healthz") and method == "GET":
            body = json.dumps({
                "status": "healthy",
                "agent": "FaqResolverAgent",
                "model_role": self.agent.model_role,
                "resolved_model_id": self.agent.resolved_model_id,
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("utf-8")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        if path == "/card" and method == "GET":
            body = json.dumps(FAQ_RESOLVER_AGENT_CARD).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("utf-8")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        if path in ("/a2a", "/resolve", "/a2a/handoff") and method == "POST":
            # Extract headers from ASGI scope
            raw_headers = scope.get("headers", [])
            headers_dict: Dict[str, str] = {}
            for h_k, h_v in raw_headers:
                headers_dict[h_k.decode("latin-1").lower()] = h_v.decode("latin-1")

            try:
                verify_auth_headers(headers_dict, self.settings)
            except PermissionError as auth_err:
                err_code = 403 if "ALLOWED_CALLERS" in str(auth_err) else 401
                err_body = json.dumps({"detail": str(auth_err)}).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": err_code,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": err_body})
                return

            body_bytes = b""
            more_body = True
            while more_body:
                msg = await receive()
                body_bytes += msg.get("body", b"")
                more_body = msg.get("more_body", False)

            try:
                payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception as e:
                err_body = json.dumps({"error": f"Invalid JSON payload: {e}"}).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": err_body})
                return

            res = handle_a2a_request(payload, agent=self.agent)
            resp_body = json.dumps(res).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(resp_body)).encode("utf-8")),
                ],
            })
            await send({"type": "http.response.body", "body": resp_body})
            return

        # 404 Not Found
        not_found_body = json.dumps({"error": f"Route '{path}' not found."}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": not_found_body})


def create_app(
    agent: Optional[FaqResolverAgent] = None,
    settings: Optional[Settings] = None,
) -> Any:
    """Create the appropriate HTTP ASGI application for the environment."""
    if _FASTAPI_AVAILABLE:
        return create_fastapi_app(agent=agent, settings=settings)
    fallback = AsgiFallbackApp(agent=agent, settings=settings)
    return fallback.handle_request


# Export default app instance for uvicorn
app = create_app(agent=root_agent)
