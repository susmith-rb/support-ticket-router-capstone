"""ADK BaseAgent wrapper for TriageRouterAgent on Vertex AI Agent Engine.

Fulfills REQ-AGENT-001, REQ-AGENT-017:
- ADK BaseAgent subclass running tiered triage policy, MCP upsert, and A2A dispatch.
- Lazy dependency resolution on first invocation (zero env validation at import/pickle time).
- Exports root_agent and app (vertexai.agent_engines.AdkApp) with import guards.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any, Optional
from pydantic import PrivateAttr

from ticket_router.agents.triage_router.agent import TriageResult, TriageRouterAgent

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


def extract_customer_id(ctx: Any) -> str:
    """Extract customer / user ID safely from invocation context without reflection."""
    try:
        session = ctx.session
        return str(session.user_id)
    except AttributeError:
        pass
    try:
        return str(ctx.user_id)
    except AttributeError:
        pass
    return "cust_anon"


def extract_answer_from_result(result: TriageResult) -> str:
    """Extract final user-facing text from TriageResult for response event."""
    if result.decision.decision == "escalate":
        return result.calming_ack or "Your request has been routed to our specialist team."
    if result.handoff_response:
        ans = result.handoff_response.get("answer")
        if ans:
            return str(ans)
        status = result.handoff_response.get("status")
        if status:
            return f"Your request has been routed for automated resolution (status: {status})."
    return "Your request has been received and processed."


def create_event(
    author: str,
    invocation_id: str,
    branch: Optional[str],
    final_text: str,
    result: TriageResult,
) -> Any:
    """Create an ADK Event or fallback shim containing final answer content."""
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
        output={
            "ticket_id": result.ticket_id,
            "decision": result.decision.decision,
            "queue": result.decision.queue,
            "answer": final_text,
        },
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
        self._router = router

    def _get_router(self) -> TriageRouterAgent:
        """Lazily initialize TriageRouterAgent on first invocation."""
        if self._router is None:
            self._router = TriageRouterAgent(lazy_init=False)
        return self._router

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        """Execute async agent invocation yielding final response event."""
        message = extract_text_from_context(ctx)
        session_id = extract_session_id(ctx)
        customer_id = extract_customer_id(ctx)

        router = self._get_router()
        result = router.triage(
            session_id=session_id,
            customer_id=customer_id,
            message_text=message,
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
        yield event

    def query(
        self,
        message: str,
        session_id: str = "default_session",
        customer_id: str = "cust_anon",
    ) -> dict[str, Any]:
        """Direct synchronous query entrypoint for local execution and smoke tests."""
        router = self._get_router()
        result = router.triage(
            session_id=session_id,
            customer_id=customer_id,
            message_text=message,
        )
        answer = extract_answer_from_result(result)
        return {
            "ticket_id": result.ticket_id,
            "session_id": result.session_id,
            "decision": result.decision.decision,
            "queue": result.decision.queue,
            "calming_ack": result.calming_ack,
            "intent": result.decision.predicted_intent,
            "answer": answer,
            "latency_ms": result.latency_ms,
        }

    def stream_query(
        self,
        message: str,
        session_id: str = "default_session",
        customer_id: str = "cust_anon",
    ) -> Iterator[dict[str, Any]]:
        """Streaming query generator for compatibility with ReasoningEngine interfaces."""
        yield self.query(message=message, session_id=session_id, customer_id=customer_id)

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
