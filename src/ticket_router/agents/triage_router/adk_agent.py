"""ADK BaseAgent wrapper for TriageRouterAgent on Vertex AI Agent Engine.

Fulfills REQ-AGENT-001, REQ-AGENT-017:
- ADK BaseAgent subclass running tiered triage policy, MCP upsert, and A2A dispatch.
- Lazy dependency resolution on first invocation (zero env validation at import/pickle time).
- Exports root_agent and app (vertexai.agent_engines.AdkApp) with import guards.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Iterator
from typing import Any, Optional

from pydantic import PrivateAttr

from ticket_router.agents.triage_router.agent import TriageResult, TriageRouterAgent
from ticket_router.agents.triage_router.templates import render_card_data_blocked_reply
from ticket_router.common.observability import configure_tracing, flush_traces
from ticket_router.security.pii_tokens import restore_pii_tokens

# Invariant: Force GenAI auto-instrumentation to never capture raw message content
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")

logger = logging.getLogger(__name__)

try:
    from google.adk.agents.base_agent import BaseAgent
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event
    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False

    class BaseAgent:
        """Minimal BaseAgent shim for environments where google-adk is not installed."""

        name: str = "triage_router"
        description: str = "Support Ticket Router Triage Agent"

        def __init__(self, name: str = "triage_router", description: str = "") -> None:
            self.name = name
            self.description = description

    class Event:
        """Minimal Event shim for environments where google-adk is not installed."""

        def __init__(
            self,
            author: str = "triage_router",
            invocation_id: str = "",
            branch: Optional[str] = None,
            content: Any = None,
            output: Any = None,
        ) -> None:
            self.author = author
            self.invocation_id = invocation_id
            self.branch = branch
            self.content = content
            self.output = output

    InvocationContext = Any


def extract_text_from_context(ctx: Any) -> str:
    """Extract inbound user message string safely without reflection."""
    try:
        content = ctx.user_content
    except AttributeError:
        content = None

    if content is None:
        return ""
    if isinstance(content, str):
        return content

    try:
        parts = content.parts
    except AttributeError:
        parts = None

    if parts:
        collected: list[str] = []
        for p in parts:
            try:
                t = p.text
                if t:
                    collected.append(t)
            except AttributeError:
                pass
        return " ".join(collected)
    return str(content)


def extract_session_id(ctx: Any) -> str:
    """Extract session ID safely from invocation context without reflection."""
    try:
        session = ctx.session
        return str(session.id)
    except AttributeError:
        pass
    try:
        return str(ctx.session_id)
    except AttributeError:
        pass
    return "default_session"


def extract_caller_user_id_for_audit(ctx: Any) -> str:
    """Extract caller-asserted user ID from invocation context for audit logging ONLY.

    Security Invariant (REQ-IDENT-007):
    This value is non-authoritative and caller-asserted. It MUST NEVER be used for
    authorization, row-level security, ticket attribution, or customer identity resolution.
    """
    if ctx is None:
        return "anon"
    try:
        session = ctx.session
        return str(session.user_id)
    except AttributeError:
        pass
    try:
        return str(ctx.user_id)
    except AttributeError:
        pass
    return "anon"


def extract_customer_id_token(ctx: Any) -> Optional[str]:
    """Extract raw customer ID token from session state or message metadata.

    Security Invariant (REQ-IDENT-003, REQ-IDENT-007):
    Token is read ONLY from session state key 'customer_id_token' or message metadata.
    Caller-asserted session user_id or any client-asserted claim in state is NEVER trusted as verified identity.
    """
    if ctx is None:
        return None

    # 1. Check session state (ADK ctx.session.state)
    try:
        session = getattr(ctx, "session", None)
        if session is not None:
            state = getattr(session, "state", None)
            if isinstance(state, dict):
                token = state.get("customer_id_token")
                if token and isinstance(token, str) and token.strip():
                    return token.strip()
            elif hasattr(state, "get"):
                token = state.get("customer_id_token")
                if token and isinstance(token, str) and token.strip():
                    return token.strip()
    except Exception:
        pass

    # 2. Check message metadata
    try:
        meta = getattr(ctx, "metadata", None)
        if isinstance(meta, dict):
            token = meta.get("customer_id_token")
            if token and isinstance(token, str) and token.strip():
                return token.strip()
    except Exception:
        pass

    try:
        user_content = getattr(ctx, "user_content", None)
        if user_content is not None:
            meta = getattr(user_content, "metadata", None)
            if isinstance(meta, dict):
                token = meta.get("customer_id_token")
                if token and isinstance(token, str) and token.strip():
                    return token.strip()
    except Exception:
        pass

    return None


DEFAULT_GE_OAUTH_AUTH_ID = "support-customer-oauth"


def extract_oauth_access_token(ctx: Any, auth_id: Optional[str] = None) -> Optional[str]:
    """Extract Google OAuth access token from ADK session state (REQ-IDENT-009).

    Inspects session state under temp:<target_id> or <target_id>.
    Never inspects non-authoritative session caller identifiers.
    """
    if ctx is None:
        return None
    target_id = (auth_id or os.environ.get("GE_OAUTH_AUTH_ID") or DEFAULT_GE_OAUTH_AUTH_ID).strip()
    session = getattr(ctx, "session", None)
    if session is None:
        return None
    state = getattr(session, "state", None)
    if state is None:
        return None

    val = None
    if isinstance(state, dict):
        val = state.get(f"temp:{target_id}")
        if val is None:
            val = state.get(target_id)
    elif hasattr(state, "get"):
        val = state.get(f"temp:{target_id}")
        if val is None:
            val = state.get(target_id)

    if val is None:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, dict):
        token_candidate = val.get("access_token")
        if isinstance(token_candidate, str) and token_candidate.strip():
            return token_candidate.strip()
    return None


def extract_answer_from_result(result: TriageResult) -> str:
    """Extract final user-facing text from TriageResult for response event."""
    ans = ""
    if result.decision.decision == "blocked":
        ans = result.calming_ack or render_card_data_blocked_reply()
    elif result.decision.decision == "escalate":
        ans = result.calming_ack or "Your request has been routed to our specialist team."
    elif result.decision.decision == "reject_oos":
        ans = result.calming_ack or "I can only assist with Global Retail Hub customer support inquiries."
    elif result.handoff_response:
        if result.handoff_response.get("answer"):
            ans = str(result.handoff_response.get("answer"))
        elif result.handoff_response.get("calming_ack"):
            ans = str(result.handoff_response.get("calming_ack"))
        elif result.handoff_response.get("message"):
            ans = str(result.handoff_response.get("message"))
        elif result.handoff_response.get("status"):
            ans = f"Your request has been routed for automated resolution (status: {result.handoff_response.get('status')})."
    if not ans:
        ans = "Your request has been received and processed."

    pii_map = getattr(result.decision, "pii_mapping", None) or {}
    return restore_pii_tokens(ans, pii_map)


def create_event(
    author: str,
    invocation_id: str,
    branch: Optional[str],
    final_text: str,
    result: TriageResult,
) -> Any:
    """Create an ADK Event or fallback shim containing final answer content."""
    output_dict: dict[str, Any] = {
        "ticket_id": result.ticket_id,
        "decision": result.decision.decision,
        "queue": result.decision.queue,
        "answer": final_text,
    }
    if result.status_code != 200:
        output_dict["status_code"] = result.status_code
    if result.error:
        output_dict["error"] = result.error

    if _HAS_ADK:
        try:
            from google.genai import types as genai_types
            content = genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=final_text)],
            )
        except Exception:
            content = {"role": "model", "parts": [{"text": final_text}]}

        return Event(
            author=author,
            invocation_id=invocation_id,
            branch=branch,
            content=content,
        )

    return Event(
        author=author,
        invocation_id=invocation_id,
        branch=branch,
        content={"role": "model", "parts": [{"text": final_text}]},
        output=output_dict,
    )


class TriageRouterAdkAgent(BaseAgent):
    """ADK BaseAgent subclass for TriageRouter on Vertex AI Agent Engine."""

    _router: Optional[TriageRouterAgent] = PrivateAttr(default=None)

    def __init__(
        self,
        router: Optional[TriageRouterAgent] = None,
        name: str = "triage_router",
        description: str = "Support Ticket Router Triage Agent",
    ) -> None:
        super().__init__(name=name, description=description)
        self.name = name
        self.description = description
        object.__setattr__(self, "_router", router)

    def __getattr__(self, name: str) -> Any:
        if name == "_router":
            return None
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def _get_router(self) -> TriageRouterAgent:
        """Lazily initialize TriageRouterAgent on first invocation."""
        configure_tracing(service_name="triage_router")
        router = getattr(self, "_router", None)
        if not isinstance(router, TriageRouterAgent):
            router = TriageRouterAgent(lazy_init=False)
            try:
                object.__setattr__(self, "_router", router)
            except Exception:
                pass
        return router

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        """Execute async agent invocation yielding final response event."""
        message = extract_text_from_context(ctx)
        session_id = extract_session_id(ctx)
        customer_id_token = extract_customer_id_token(ctx)
        oauth_access_token = extract_oauth_access_token(ctx)

        router = self._get_router()
        result = router.triage(
            session_id=session_id,
            message_text=message,
            customer_id_token=customer_id_token,
            oauth_access_token=oauth_access_token,
        )
        final_text = extract_answer_from_result(result)

        inv_id = ""
        try:
            inv_id = str(ctx.invocation_id)
        except AttributeError:
            pass

        br = None
        try:
            br = ctx.branch
        except AttributeError:
            pass

        event = create_event(
            author=self.name,
            invocation_id=inv_id,
            branch=br,
            final_text=final_text,
            result=result,
        )
        try:
            yield event
        finally:
            try:
                flush_traces(timeout_millis=1500)
            except Exception:
                pass

    def query(
        self,
        message: str,
        session_id: str = "default_session",
        customer_id_token: Optional[str] = None,
        customer_id: Optional[str] = None,
        oauth_access_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Direct synchronous query entrypoint for local execution and smoke tests."""
        if customer_id is not None:
            logger.warning(
                "customer_id argument is deprecated and ignored; pass customer_id_token instead."
            )
        router = self._get_router()
        result = router.triage(
            session_id=session_id,
            message_text=message,
            customer_id_token=customer_id_token,
            oauth_access_token=oauth_access_token,
        )
        flush_traces(timeout_millis=1500)
        answer = extract_answer_from_result(result)
        resp: dict[str, Any] = {
            "ticket_id": result.ticket_id,
            "session_id": result.session_id,
            "decision": result.decision.decision,
            "queue": result.decision.queue,
            "calming_ack": result.calming_ack,
            "intent": result.decision.predicted_intent,
            "answer": answer,
            "latency_ms": result.latency_ms,
        }
        if result.status_code != 200:
            resp["status_code"] = result.status_code
        if result.error:
            resp["error"] = result.error
        return resp

    def stream_query(
        self,
        message: str,
        session_id: str = "default_session",
        customer_id_token: Optional[str] = None,
        customer_id: Optional[str] = None,
        oauth_access_token: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        """Streaming query generator for compatibility with ReasoningEngine interfaces."""
        yield self.query(
            message=message,
            session_id=session_id,
            customer_id_token=customer_id_token,
            customer_id=customer_id,
            oauth_access_token=oauth_access_token,
        )

    def triage(self, *args: Any, **kwargs: Any) -> TriageResult:
        """Direct delegation to underlying TriageRouterAgent."""
        return self._get_router().triage(*args, **kwargs)


root_agent = TriageRouterAdkAgent()


def build_adk_app() -> Any:
    """Build AdkApp instance if vertexai is installed, else return None."""
    try:
        from vertexai.agent_engines.templates.adk import AdkApp
        return AdkApp(agent=root_agent, enable_tracing=True)
    except (ImportError, Exception):
        return None


app = build_adk_app()
