"""app.py — HTTP serving entrypoint for FaqResolverAgent on GKE Autopilot.

REQ-AGENT-001, REQ-AGENT-006, REQ-AGENT-007.
Exposes /health and /healthz (liveness/readiness), /card (A2A discovery), and /a2a / /resolve
(inquiry processing). Provides native FastAPI app when installed, with a zero-dependency
ASGI fallback so lightweight containers and test runners without FastAPI can import and run.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from ticket_router.agents.faq_resolver.a2a import (
    FAQ_RESOLVER_AGENT_CARD,
    handle_a2a_request,
)
from ticket_router.agents.faq_resolver.agent import FaqResolverAgent, root_agent

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


router = APIRouter()


def get_active_agent(request: Request) -> FaqResolverAgent:
    try:
        return request.app.state.agent
    except AttributeError:
        return root_agent


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
async def resolve_endpoint(
    request: Request,
    active_agent: FaqResolverAgent = Depends(get_active_agent),
) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid JSON payload: {e}"
        ) from e

    return handle_a2a_request(payload, agent=active_agent)


def create_fastapi_app(agent: Optional[FaqResolverAgent] = None) -> Any:
    """Build and return a FastAPI application for GKE Autopilot container serving."""
    application = FastAPI(
        title="FaqResolverAgent Service",
        version="1.0.0",
        description="GKE Autopilot microservice for bounded ReAct FAQ resolution with citations.",
    )
    application.state.agent = agent if agent is not None else root_agent
    application.include_router(router)
    return application


class AsgiFallbackApp:
    """Lightweight pure-Python ASGI application for environments without FastAPI."""

    def __init__(self, agent: Optional[FaqResolverAgent] = None) -> None:
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

        if path in ("/a2a", "/resolve") and method == "POST":
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


def create_app(agent: Optional[FaqResolverAgent] = None) -> Any:
    """Create the appropriate HTTP ASGI application for the environment."""
    if _FASTAPI_AVAILABLE:
        return create_fastapi_app(agent=agent)
    fallback = AsgiFallbackApp(agent=agent)
    return fallback.handle_request


# Export default app instance for uvicorn
app = create_app()
