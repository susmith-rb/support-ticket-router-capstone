"""Tests for TriageRouterAdkAgent and ADK Agent Engine integration.

Fulfills REQ-AGENT-001, REQ-AGENT-017:
- Verify BaseAgent subclass run_async / query / stream_query lifecycle.
- Injected fakes for FAQ auto-RAG route and angry/urgent escalation route.
- Verify lazy initialization safely defers dependency validation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ticket_router.agents.triage_router.a2a_client import InMemoryA2AClient
from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    extract_customer_id,
    extract_session_id,
    extract_text_from_context,
    root_agent,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
)
from ticket_router.models.router_s1_client import RouterPrediction, RouterS1Client


class DummyPart:
    def __init__(self, text: str) -> None:
        self.text = text


class DummyContent:
    def __init__(self, text: str) -> None:
        self.parts = [DummyPart(text)]


class DummySession:
    def __init__(self, session_id: str, user_id: str) -> None:
        self.id = session_id
        self.user_id = user_id


class DummyContext:
    def __init__(self, text: str, session_id: str, user_id: str, invocation_id: str = "inv-1") -> None:
        self.user_content = DummyContent(text)
        self.session = DummySession(session_id, user_id)
        self.invocation_id = invocation_id
        self.branch = None


def test_context_extraction_helpers():
    """Verify context extraction helpers handle objects safely without reflection."""
    ctx = DummyContext("Hello support", "sess-123", "cust-456", "inv-999")
    assert extract_text_from_context(ctx) == "Hello support"
    assert extract_session_id(ctx) == "sess-123"
    assert extract_customer_id(ctx) == "cust-456"

    # Fallbacks on empty / None
    assert extract_text_from_context(None) == ""
    assert extract_session_id(None) == "default_session"
    assert extract_customer_id(None) == "cust_anon"


def test_lazy_initialization_no_env_at_instantiation(monkeypatch):
    """Verify instantiating TriageRouterAdkAgent requires zero environment variables."""
    monkeypatch.setenv("REQUIRE_REAL_DEPS", "1")
    monkeypatch.delenv("ROUTER_S1_ENDPOINT", raising=False)
    monkeypatch.delenv("TICKET_DB_MCP_URL", raising=False)

    # Instantiation must not raise
    agent = TriageRouterAdkAgent()
    assert agent.name == "triage_router"
    assert agent._router is None


def test_adk_agent_query_and_stream_faq_route():
    """Verify FAQ auto-RAG route returns automated resolution answer."""
    in_mem_mcp = InMemoryTicketDbMcpClient()
    in_mem_a2a = InMemoryA2AClient(
        response_generator=lambda payload: {
            "status": "RESOLVED",
            "answer": "Our standard return window is 30 days.",
            "citations": [{"title": "Return Policy"}],
        }
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(tau_esc=0.50, tau_intent=0.70),
        s1_client=RouterS1Client(),
        tier2_invoker=lambda text: RouterPrediction(
            escalate=False,
            escalate_prob=0.05,
            sentiment="neutral",
            sentiment_prob=0.90,
            urgency="normal",
            urgency_prob=0.90,
            intent="return_policy",
            intent_prob=0.95,
        ),
    )

    core_router = TriageRouterAgent(
        policy=policy,
        mcp_client=in_mem_mcp,
        a2a_client=in_mem_a2a,
        lazy_init=False,
    )
    adk_agent = TriageRouterAdkAgent(router=core_router)

    # 1. query()
    res = adk_agent.query("What is the return window?", session_id="s-faq-1", customer_id="c-faq-1")
    assert res["decision"] == "auto_rag"
    assert "30 days" in res["answer"]
    assert in_mem_mcp.tickets  # Ticket record created

    # 2. stream_query()
    stream_results = list(adk_agent.stream_query("What is the return window?", session_id="s-faq-2"))
    assert len(stream_results) == 1
    assert stream_results[0]["decision"] == "auto_rag"


async def collect_agent_events(agent: TriageRouterAdkAgent, ctx: Any) -> list[Any]:
    """Helper collecting events from agent async generator at module scope."""
    events = []
    async for event in agent._run_async_impl(ctx):
        events.append(event)
    return events


def extract_event_text(event: Any) -> str:
    """Helper extracting text from event without reflection."""
    try:
        content = event.content
    except AttributeError:
        content = None

    if content is not None:
        try:
            parts = content.parts
            if parts:
                return str(parts[0].text)
        except AttributeError:
            pass

        if isinstance(content, dict):
            return str(content.get("parts", [{}])[0].get("text", ""))
        return str(content)

    try:
        output = event.output
        if isinstance(output, dict):
            return str(output.get("answer", ""))
    except AttributeError:
        pass

    return ""


def test_adk_agent_run_async_escalation_route():
    """Verify angry/urgent message routes to escalation with calming ack."""
    in_mem_mcp = InMemoryTicketDbMcpClient()
    in_mem_a2a = InMemoryA2AClient()

    policy = TriagePolicy(
        thresholds=RouterThresholds(tau_esc=0.50),
        s1_client=RouterS1Client(),
        tier2_invoker=lambda text: RouterPrediction(
            escalate=True,
            escalate_prob=0.95,
            sentiment="angry",
            sentiment_prob=0.95,
            urgency="immediate",
            urgency_prob=0.90,
            intent="billing",
            intent_prob=0.85,
        ),
    )

    core_router = TriageRouterAgent(
        policy=policy,
        mcp_client=in_mem_mcp,
        a2a_client=in_mem_a2a,
        lazy_init=False,
    )
    adk_agent = TriageRouterAdkAgent(router=core_router)

    ctx = DummyContext(
        text="I was charged twice and I demand a supervisor right now!",
        session_id="s-esc-1",
        user_id="c-esc-1",
        invocation_id="inv-esc-1",
    )

    events = asyncio.run(collect_agent_events(adk_agent, ctx))
    assert len(events) == 1
    event = events[0]
    assert event.author == "triage_router"

    text = extract_event_text(event)
    assert "ticket" in text.lower() or "support team" in text.lower() or "representative" in text.lower()
    # Verify ticket upserted
    assert len(in_mem_mcp.tickets) == 1
    upserted = list(in_mem_mcp.tickets.values())[0]
    assert upserted["decision"]["decision"] == "escalate"
    assert upserted["customer_id"] == "c-esc-1"


def test_exported_root_agent():
    """Verify module-level root_agent exists and has correct metadata."""
    assert root_agent is not None
    assert root_agent.name == "triage_router"
    assert "Triage" in root_agent.description


def test_adk_agent_cloudpickle_private_attr_recovery():
    """Verify TriageRouterAdkAgent recovers _router gracefully if stripped during cloudpickle deserialization."""
    agent = TriageRouterAdkAgent()
    if hasattr(agent, "__pydantic_private__") and agent.__pydantic_private__:
        agent.__pydantic_private__.pop("_router", None)
    agent.__dict__.pop("_router", None)

    resolved_router = agent._get_router()
    assert isinstance(resolved_router, TriageRouterAgent)
    assert agent._router is resolved_router

