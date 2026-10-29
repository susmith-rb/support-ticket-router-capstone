"""Tests for concurrent turn I/O, non-fatal ticket upsert, and post-response trace flush.

# REQ-AGENT-027: Concurrent Turn I/O, Non-Fatal Ticket Persistence, and Post-Response Trace Flush
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

import pytest

from ticket_router.agents.triage_router import adk_agent as adk_agent_mod
from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload, InMemoryA2AClient
from ticket_router.agents.triage_router.adk_agent import TriageRouterAdkAgent
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import RouterDecision, TriagePolicy
from ticket_router.common.observability import get_current_traceparent, is_pii_clean
from ticket_router.identity import ResolvedCustomerContext


class _DummyPart:
    def __init__(self, text: str) -> None:
        self.text = text


class _DummyContent:
    def __init__(self, text: str) -> None:
        self.parts = [_DummyPart(text)]


class _DummySession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.state: dict[str, Any] = {}


class _DummyContext:
    def __init__(self, text: str, session_id: str = "sess-adk-1") -> None:
        self.user_content = _DummyContent(text)
        self.session = _DummySession(session_id)
        self.invocation_id = "inv-027"
        self.branch = None


# REQ-AGENT-027
def test_triage_parallel_io_and_nonfatal_upsert(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Acceptance test:
    1. resolve_identity runs concurrently with policy.evaluate (propagating trace context).
    2. On auto_rag A2A path, upsert_ticket runs concurrently with a2a_client.handoff.
    3. Upsert failing on both attempts retries once, logs PII-free ticket_upsert_failed warning,
       and never fails the customer turn.
    """
    sleep_s = 0.20
    traceparents_seen: dict[str, Optional[str]] = {}
    lock = threading.Lock()

    def slow_resolve_identity(
        token: Optional[str] = None,
        ticket_db_client: Any = None,
        **kwargs: Any,
    ) -> ResolvedCustomerContext:
        with lock:
            traceparents_seen["resolve_identity"] = get_current_traceparent()
        time.sleep(sleep_s)
        return ResolvedCustomerContext(
            customer_id="cust_parallel_1",
            verified=True,
            issuer="https://accounts.google.com",
            reason="ok",
        )

    monkeypatch.setattr("ticket_router.agents.triage_router.agent.resolve_identity", slow_resolve_identity)

    class SlowPolicy:
        def evaluate(
            self,
            text: str,
            session_id: str,
            customer_id: str,
            verified_facts: Any = None,
            ticket_id: Optional[str] = None,
        ) -> RouterDecision:
            with lock:
                traceparents_seen["policy_evaluate"] = get_current_traceparent()
            # Placeholder neutral customer_id passed before identity finishes
            assert customer_id == f"anon:{session_id}"
            time.sleep(sleep_s)
            return RouterDecision(
                ticket_id=ticket_id or "TICK-PAR",
                session_id=session_id,
                decision="auto_rag",
                tier="tier1_router_s1",
                queue="none",
                predicted_intent="shipping_policy",
                escalate=False,
                sentiment="neutral",
                urgency="normal",
                reason="tier1_definitive_auto_rag",
            )

    class SlowMcpClient(InMemoryTicketDbMcpClient):
        def __init__(self, fail_upsert: bool = False) -> None:
            super().__init__()
            self.fail_upsert = fail_upsert
            self.upsert_calls = 0
            self.upsert_completed = False

        def upsert_ticket(
            self,
            idempotency_key: str,
            customer_id: str,
            channel: str,
            body_redacted: str,
            decision: Dict[str, Any],
        ) -> Dict[str, Any]:
            with lock:
                self.upsert_calls += 1
                traceparents_seen["upsert_ticket"] = get_current_traceparent()
            if self.fail_upsert:
                raise TimeoutError("Read timed out calling ticket-db-mcp (secret@victim.org)")
            time.sleep(sleep_s)
            res = super().upsert_ticket(
                idempotency_key=idempotency_key,
                customer_id=customer_id,
                channel=channel,
                body_redacted=body_redacted,
                decision=decision,
            )
            self.upsert_completed = True
            return res

    class SlowA2AClient:
        def __init__(self) -> None:
            self.received_payload: Optional[A2AHandoffPayload] = None

        def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
            self.received_payload = payload
            time.sleep(sleep_s)
            return {
                "status": "AUTO_RESOLVED",
                "route": "resolved",
                "answer": "Standard shipping takes 3 to 5 business days.",
            }

    # --- Part A: Overlap of (resolve_identity || policy.evaluate) and (upsert_ticket || a2a_client.handoff) ---
    slow_mcp = SlowMcpClient(fail_upsert=False)
    slow_a2a = SlowA2AClient()
    agent = TriageRouterAgent(
        policy=SlowPolicy(),  # type: ignore[arg-type]
        mcp_client=slow_mcp,
        a2a_client=slow_a2a,
    )

    t0 = time.monotonic()
    res = agent.triage(
        session_id="sess_par_01",
        message_text="How long does standard shipping take?",
        ticket_id="TICK-PAR-01",
        oauth_access_token="oauth-tok",
    )
    wall_s = time.monotonic() - t0

    # Serial execution of 4 x 0.20s sleeps would take >= 0.80s; parallel pairs take ~0.40s.
    assert wall_s < 0.65, f"Expected parallel overlap (<0.65s for 0.80s of work), got {wall_s:.3f}s"
    assert slow_mcp.upsert_completed is True
    assert slow_mcp.upsert_calls == 1
    assert res.decision.customer_verified is True
    assert res.decision.identity_issuer == "https://accounts.google.com"
    assert res.handoff_response is not None
    assert res.handoff_response["answer"] == "Standard shipping takes 3 to 5 business days."
    assert slow_a2a.received_payload is not None
    assert slow_a2a.received_payload.customer_id == "cust_parallel_1"

    # ContextVars (traceparent) propagated to all worker threads under same 128-bit traceId
    tp_id = traceparents_seen["resolve_identity"]
    tp_pol = traceparents_seen["policy_evaluate"]
    tp_ups = traceparents_seen["upsert_ticket"]
    assert tp_id is not None and tp_pol is not None and tp_ups is not None
    assert tp_id.split("-")[1] == tp_pol.split("-")[1] == tp_ups.split("-")[1]

    # --- Part B: Non-fatal upsert (retries once -> 2 attempts, logs PII-free warning, returns answer) ---
    failing_mcp = SlowMcpClient(fail_upsert=True)
    agent_nonfatal = TriageRouterAgent(
        policy=SlowPolicy(),  # type: ignore[arg-type]
        mcp_client=failing_mcp,
        a2a_client=SlowA2AClient(),
    )

    secret_customer_text = "My email is alice@example.com and my order ORD-10001 is delayed"
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res_nf = agent_nonfatal.triage(
            session_id="sess_nf_01",
            message_text=secret_customer_text,
            ticket_id="TICK-NF-01",
        )

    assert failing_mcp.upsert_calls == 2  # Initial attempt + 1 retry
    assert res_nf.decision.decision == "auto_rag"
    assert res_nf.handoff_response is not None
    assert "3 to 5 business days" in res_nf.handoff_response["answer"]

    warn_records = [r for r in caplog.records if "ticket_upsert_failed" in r.getMessage()]
    assert len(warn_records) == 1
    warn_msg = warn_records[0].getMessage()
    parsed_warn = json.loads(warn_msg)
    assert parsed_warn == {
        "event": "ticket_upsert_failed",
        "ticket_id": "TICK-NF-01",
        "error_type": "TimeoutError",
    }
    assert is_pii_clean(parsed_warn)
    assert "alice@example.com" not in warn_msg
    assert "secret@victim.org" not in warn_msg
    assert "ORD-10001" not in warn_msg


# REQ-AGENT-027
def test_nonfatal_upsert_across_escalate_greeting_oos_and_a2a_degraded(caplog: pytest.LogCaptureFixture):
    """Verify non-fatal _safe_upsert retry and fallback across greeting, escalate, reject_oos, and A2A failure paths."""

    class AlwaysTimeoutMcp(InMemoryTicketDbMcpClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def upsert_ticket(self, **kwargs: Any) -> Dict[str, Any]:
            self.calls += 1
            raise TimeoutError("ticket-db-mcp cold start timeout")

    # 1. Greeting path: 1 _safe_upsert (2 attempts), returns friendly reply
    mcp_greet = AlwaysTimeoutMcp()
    agent_greet = TriageRouterAgent(
        policy=TriagePolicy(),
        mcp_client=mcp_greet,
        a2a_client=InMemoryA2AClient(),
    )
    with caplog.at_level(logging.WARNING):
        res_greet = agent_greet.triage(session_id="s-greet", message_text="hello!", ticket_id="TICK-GR-1")
    assert mcp_greet.calls == 2
    assert res_greet.handoff_response is not None
    assert res_greet.handoff_response["status"] == "AUTO_RESOLVED"

    # 2. Escalate path: 1 _safe_upsert (2 attempts), returns calming ack
    mcp_esc = AlwaysTimeoutMcp()
    agent_esc = TriageRouterAgent(
        policy=TriagePolicy(),
        mcp_client=mcp_esc,
        a2a_client=InMemoryA2AClient(),
    )
    with caplog.at_level(logging.WARNING):
        res_esc = agent_esc.triage(
            session_id="s-esc",
            message_text="I want to speak to a human representative now",
            ticket_id="TICK-ESC-1",
        )
    assert mcp_esc.calls == 2
    assert res_esc.decision.decision == "escalate"
    assert res_esc.calming_ack is not None and "TICK-ESC-1" in res_esc.calming_ack

    # 3. reject_oos path: 1 _safe_upsert (2 attempts), returns scope refusal
    class OosPolicy:
        def evaluate(self, **kwargs: Any) -> RouterDecision:
            return RouterDecision(
                ticket_id=kwargs.get("ticket_id") or "TICK-OOS-1",
                session_id=kwargs["session_id"],
                decision="reject_oos",
                tier="tier2_flash",
                queue="none",
                predicted_intent="other",
                escalate=False,
                sentiment="neutral",
                urgency="normal",
                reason="tier2_out_of_scope",
            )

    mcp_oos = AlwaysTimeoutMcp()
    agent_oos = TriageRouterAgent(
        policy=OosPolicy(),  # type: ignore[arg-type]
        mcp_client=mcp_oos,
        a2a_client=InMemoryA2AClient(),
    )
    res_oos = agent_oos.triage(session_id="s-oos", message_text="stock price?", ticket_id="TICK-OOS-1")
    assert mcp_oos.calls == 2
    assert res_oos.decision.decision == "reject_oos"
    assert res_oos.calming_ack is not None

    # 4. A2A handoff failure path: initial upsert (2 attempts) + secondary degraded upsert (2 attempts) = 4 calls
    class AutoRagPolicy:
        def evaluate(self, **kwargs: Any) -> RouterDecision:
            return RouterDecision(
                ticket_id=kwargs.get("ticket_id") or "TICK-DEG-1",
                session_id=kwargs["session_id"],
                decision="auto_rag",
                tier="tier1_router_s1",
                queue="none",
                predicted_intent="shipping_policy",
                escalate=False,
                sentiment="neutral",
                urgency="normal",
                reason="tier1_definitive_auto_rag",
            )

    class FailingA2A:
        def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
            raise ConnectionError("a2a unreachable")

    mcp_deg = AlwaysTimeoutMcp()
    agent_deg = TriageRouterAgent(
        policy=AutoRagPolicy(),  # type: ignore[arg-type]
        mcp_client=mcp_deg,
        a2a_client=FailingA2A(),
    )
    res_deg = agent_deg.triage(session_id="s-deg", message_text="shipping rates?", ticket_id="TICK-DEG-1")
    assert mcp_deg.calls == 4
    assert res_deg.decision.decision == "escalate"
    assert res_deg.decision.degraded is True
    assert res_deg.handoff_response is not None
    assert res_deg.handoff_response["reason"] == "a2a_handoff_failed"


# REQ-AGENT-027
def test_adk_run_async_impl_yields_event_before_flush_traces(monkeypatch: pytest.MonkeyPatch):
    """Verify TriageRouterAdkAgent._run_async_impl yields response event before calling flush_traces."""
    order: list[str] = []

    def fake_flush_traces(timeout_millis: int = 1500) -> bool:
        order.append(f"flush_traces:{timeout_millis}")
        raise RuntimeError("simulated exporter failure must be swallowed")

    monkeypatch.setattr(adk_agent_mod, "flush_traces", fake_flush_traces)

    core_router = TriageRouterAgent(
        policy=TriagePolicy(),
        mcp_client=InMemoryTicketDbMcpClient(),
        a2a_client=InMemoryA2AClient(),
    )
    adk_agent = TriageRouterAdkAgent(router=core_router)
    ctx = _DummyContext("hello!")

    async def _drive() -> list[Any]:
        received = []
        async for ev in adk_agent._run_async_impl(ctx):
            # At the moment the caller receives the yielded event, flush_traces MUST NOT have run yet
            assert order == [], f"flush_traces ran before yield: {order}"
            order.append("event_yielded")
            received.append(ev)
        return received

    events = asyncio.run(_drive())
    assert len(events) == 1
    assert order == ["event_yielded", "flush_traces:1500"]
