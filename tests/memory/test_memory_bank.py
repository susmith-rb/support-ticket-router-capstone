"""Unit and integration tests for Vertex AI Agent Engine Memory Bank.

# REQ-AGENT-036: Vertex AI Agent Engine Memory Bank Integration
# REQ-STORE-009: PII Tokenization and Zero Leakage to Memory Bank
# REQ-AGENT-032: Ingress Payment Card Data Block & PCI Isolation
# REQ-IDENT-003: Server-Side Identity Verification & Scoping
"""

from __future__ import annotations

import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload, InMemoryA2AClient
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    RouterPrediction,
    RouterThresholds,
    TriagePolicy,
)
from ticket_router.identity import clear_identity_cache
from ticket_router.memory.client import (
    DEFAULT_MEMORY_BANK_ENGINE,
    FakeMemoryBankClient,
    VertexMemoryBankClient,
    is_memory_bank_enabled,
    render_memory_summary,
)
from ticket_router.models.router_s1_client import RouterS1Client

PROJECT_ID = "susmith-fde-ticket-router"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"


@pytest.fixture(autouse=True)
def reset_cache_and_env(monkeypatch: pytest.MonkeyPatch):
    clear_identity_cache()
    # In tests, default is disabled unless explicitly set; enable for memory tests
    monkeypatch.setenv("MEMORY_BANK_ENABLED", "1")
    yield
    clear_identity_cache()


def _setup_verified_agent(
    memories_by_user: Dict[str, List[str]] | None = None,
    simulate_timeout: bool = False,
    simulate_error: bool = False,
    fake_s1_route: RouterPrediction | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[TriageRouterAgent, FakeMemoryBankClient, list[A2AHandoffPayload]]:
    now = time.time()

    def mock_verify_firebase(token: str, request: Any = None, audience: str = PROJECT_ID):
        if token == "token_verified_cust":
            return {
                "iss": ISSUER,
                "aud": PROJECT_ID,
                "sub": "user_sub_1001",
                "email": "customer@example.com",
                "email_verified": True,
                "exp": now + 3600.0,
            }
        raise ValueError("Invalid token")

    if monkeypatch:
        monkeypatch.setattr("google.oauth2.id_token.verify_firebase_token", mock_verify_firebase)

    db_client = InMemoryTicketDbMcpClient()
    db_client.register_identity(
        issuer=ISSUER,
        subject="user_sub_1001",
        customer_id="cust_1001",
        status="active",
        linked=True,
    )

    mem_client = FakeMemoryBankClient(
        memories_by_user=memories_by_user or {},
        simulate_timeout=simulate_timeout,
        simulate_error=simulate_error,
    )

    a2a_handoffs: list[A2AHandoffPayload] = []

    def fake_handoff(payload: A2AHandoffPayload) -> Dict[str, Any]:
        a2a_handoffs.append(payload)
        return {"status": "AUTO_RESOLVED", "route": "resolved", "answer": "Grounded answer"}

    a2a_client = InMemoryA2AClient(response_generator=fake_handoff)

    mock_s1 = MagicMock(spec=RouterS1Client)
    mock_s1.route.return_value = fake_s1_route or RouterPrediction(
        escalate=False,
        escalate_prob=0.05,
        sentiment="neutral",
        sentiment_prob=0.95,
        urgency="normal",
        urgency_prob=0.95,
        intent="shipping",
        intent_prob=0.95,
    )

    policy = TriagePolicy(
        thresholds=RouterThresholds(tau_esc=0.35),
        s1_client=mock_s1,
    )

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=db_client,
        a2a_client=a2a_client,
        memory_client=mem_client,
    )

    return agent, mem_client, a2a_handoffs


# REQ-AGENT-036
def test_verified_customer_retrieve_and_summary_injected(monkeypatch: pytest.MonkeyPatch):
    """Verified customer triggers retrieve with scope user_id; summary injected into A2A and decision."""
    agent, mem_client, handoffs = _setup_verified_agent(
        memories_by_user={
            "cust_1001": [
                "Customer prefers expedited carrier shipping",
                "Primary contact language: en-US",
            ]
        },
        monkeypatch=monkeypatch,
    )

    result = agent.triage(
        session_id="s_verified",
        message_text="Where is my shipment?",
        customer_id_token="token_verified_cust",
    )

    # 1. Retrieve was called with verified customer scope
    assert len(mem_client.retrieve_calls) == 1
    call = mem_client.retrieve_calls[0]
    assert call["customer_id"] == "cust_1001"
    assert call["scope"] == {"user_id": "cust_1001"}
    assert call["top_k"] <= 5
    assert call["timeout_s"] <= 0.350

    # 2. Summary is injected into decision details and A2A payload
    summary = result.decision.details.get("memory_summary")
    assert summary is not None
    assert "expedited carrier shipping" in summary
    assert "en-US" in summary
    assert len(summary) <= 400

    assert len(handoffs) == 1
    assert handoffs[0].memory_summary == summary

    # 3. Write was called after turn completed
    assert len(mem_client.generate_calls) == 1
    gen_call = mem_client.generate_calls[0]
    assert gen_call["customer_id"] == "cust_1001"
    assert gen_call["scope"] == {"user_id": "cust_1001"}
    assert "Decision: auto_rag" in gen_call["decision_summary"]


# REQ-AGENT-036
def test_anon_customer_no_read_or_write(monkeypatch: pytest.MonkeyPatch):
    """Anonymous callers (no token or anon: ID) NEVER read or write memories."""
    agent, mem_client, handoffs = _setup_verified_agent(
        memories_by_user={"cust_1001": ["Some secret note"]},
        monkeypatch=monkeypatch,
    )

    result = agent.triage(
        session_id="s_anon_turn",
        message_text="How do I track an order?",
        customer_id_token=None,  # Anonymous
    )

    # Zero retrieve calls and zero generate calls
    assert len(mem_client.retrieve_calls) == 0
    assert len(mem_client.generate_calls) == 0
    assert result.decision.details.get("memory_summary") is None or result.decision.details.get("memory_summary") == ""
    if handoffs:
        assert handoffs[0].memory_summary == ""


# REQ-AGENT-036
def test_timeout_fails_open(monkeypatch: pytest.MonkeyPatch):
    """Memory Bank retrieve timeout (~300 ms) fails open, returning empty summary without failing turn."""
    agent, mem_client, handoffs = _setup_verified_agent(
        memories_by_user={"cust_1001": ["Some fact"]},
        simulate_timeout=True,
        monkeypatch=monkeypatch,
    )

    result = agent.triage(
        session_id="s_timeout_test",
        message_text="What is your return policy?",
        customer_id_token="token_verified_cust",
    )

    # Retrieve was attempted
    assert len(mem_client.retrieve_calls) == 1
    # Turn completed cleanly with empty summary (fail-open)
    assert result.status_code == 200
    assert result.decision.decision == "auto_rag"
    assert result.decision.details.get("memory_summary") is None or result.decision.details.get("memory_summary") == ""
    assert handoffs[0].memory_summary == ""


# REQ-AGENT-036
# REQ-STORE-009
def test_write_uses_tokenized_text_only_assert_no_email_in_payload(monkeypatch: pytest.MonkeyPatch):
    """Write/generate memories uses tokenized text only (assert no raw email address in payload)."""
    agent, mem_client, _ = _setup_verified_agent(monkeypatch=monkeypatch)

    raw_email = "sensitive_user@secretdomain.com"
    msg_with_email = f"Please contact me at {raw_email} about my missing package."

    # Mock policy to tokenize text via Model Armor / REQ-STORE-009 tokenization
    def mock_evaluate(*args, **kwargs):
        return RouterDecision(
            ticket_id="TICK-TOK-1",
            session_id=kwargs["session_id"],
            decision="auto_rag",
            tier="tier1_router_s1",
            queue="none",
            predicted_intent="shipping",
            escalate=False,
            sentiment="neutral",
            urgency="normal",
            reason="tier1_route",
            tokenized_text="Please contact me at [EMAIL_0] about my missing package.",
            pii_mapping={"[EMAIL_0]": raw_email},
        )

    agent.policy.evaluate = mock_evaluate  # type: ignore[assignment]

    result = agent.triage(
        session_id="s_pii_tok",
        message_text=msg_with_email,
        customer_id_token="token_verified_cust",
    )

    assert result.status_code == 200
    assert len(mem_client.generate_calls) == 1
    write_payload = mem_client.generate_calls[0]

    user_text = write_payload["user_message_tokenized"]
    dec_text = write_payload["decision_summary"]

    # Critical PII Invariant: raw email must NOT appear in Memory Bank write payload
    assert raw_email not in user_text, f"Raw email leaked to Memory Bank user message: {user_text}"
    assert raw_email not in dec_text, f"Raw email leaked to Memory Bank decision summary: {dec_text}"
    assert "[EMAIL_0]" in user_text, f"Expected tokenized text in write payload: {user_text}"


# REQ-AGENT-036
# REQ-AGENT-032
def test_card_blocked_turn_no_write(monkeypatch: pytest.MonkeyPatch):
    """Ingress payment card blocked turns (REQ-MA-007) MUST NOT write to Memory Bank."""
    agent, mem_client, _ = _setup_verified_agent(monkeypatch=monkeypatch)

    class CardBlockedPolicy:
        model_armor_client = MagicMock()

        def _run_model_armor(self, text: str):
            from ticket_router.security.model_armor import ModelArmorVerdict, SdpFinding
            v = ModelArmorVerdict(
                is_clean=False,
                blocked=True,
                action="BLOCK",
                sanitized_text=text,
                sdp_findings=[
                    SdpFinding(
                        info_type="CREDIT_CARD_NUMBER",
                        location={"codepointRange": {"start": 0, "end": 10}},
                    )
                ],
            )
            return v, None, None, {}

        def evaluate(self, *args, **kwargs):
            raise AssertionError("evaluate should not be reached when card is blocked at ingress")

    agent._policy = CardBlockedPolicy()  # type: ignore[assignment]

    result = agent.triage(
        session_id="s_card_blocked",
        message_text="My card number is 4111 1111 1111 1111 please charge it",
        customer_id_token="token_verified_cust",
    )

    assert result.decision.decision == "blocked"
    assert result.status_code == 400

    # Ingress card guard invariant: ZERO calls to Memory Bank
    assert len(mem_client.retrieve_calls) == 0
    assert len(mem_client.generate_calls) == 0


# REQ-AGENT-036
def test_flag_off_no_calls(monkeypatch: pytest.MonkeyPatch):
    """When MEMORY_BANK_ENABLED=0, no retrieve or generate calls are made."""
    monkeypatch.setenv("MEMORY_BANK_ENABLED", "0")
    agent, mem_client, handoffs = _setup_verified_agent(
        memories_by_user={"cust_1001": ["Some memory"]},
        monkeypatch=monkeypatch,
    )

    assert not is_memory_bank_enabled()

    result = agent.triage(
        session_id="s_flag_off",
        message_text="Can I get an update on my package?",
        customer_id_token="token_verified_cust",
    )

    assert result.status_code == 200
    assert len(mem_client.retrieve_calls) == 0
    assert len(mem_client.generate_calls) == 0
    assert result.decision.details.get("memory_summary") is None or result.decision.details.get("memory_summary") == ""
    assert handoffs[0].memory_summary == ""


# REQ-AGENT-036
def test_render_memory_summary_formatting():
    """Verify render_memory_summary deduplication, bullet formatting, and length capping (<=400 chars)."""
    # 1. Empty or None
    assert render_memory_summary([]) == ""

    # 2. String facts with deduplication
    facts = [
        "Prefers email notifications",
        "Prefers email notifications",  # duplicate
        "Lives in Pacific timezone",
    ]
    summary = render_memory_summary(facts)
    assert summary == "- Prefers email notifications\n- Lives in Pacific timezone"
    assert len(summary) <= 400

    # 3. Object facts (mock memory objects)
    class FakeMemItem:
        def __init__(self, fact: str):
            self.memory = MagicMock()
            self.memory.fact = fact

    obj_facts = [FakeMemItem("Fact A"), FakeMemItem("Fact B")]
    assert render_memory_summary(obj_facts) == "- Fact A\n- Fact B"

    # 4. Long text truncation at <= 400 chars
    long_facts = [f"Durable fact number {i}: " + ("x" * 50) for i in range(15)]
    long_summary = render_memory_summary(long_facts)
    assert len(long_summary) <= 400
    assert long_summary.endswith("...")


# REQ-AGENT-036
def test_vertex_memory_bank_client_sdk_contract(monkeypatch: pytest.MonkeyPatch):
    """Verify VertexMemoryBankClient calls Vertex AI SDK agent_engines.memories with exact parameters."""
    mock_genai_client = MagicMock()
    mock_memories = MagicMock()
    mock_genai_client.agent_engines.memories = mock_memories

    # Mock retrieve response
    mock_retrieved_item = MagicMock()
    mock_retrieved_item.memory.fact = "Customer prefers SMS alerts"
    mock_memories.retrieve.return_value = [mock_retrieved_item]

    client = VertexMemoryBankClient(
        engine_name=DEFAULT_MEMORY_BANK_ENGINE,
        client=mock_genai_client,
    )

    # 1. Test retrieve_summary
    summary = client.retrieve_summary(
        customer_id="cust_sdk_123",
        query="order update",
        timeout_s=0.300,
        top_k=5,
    )
    assert "- Customer prefers SMS alerts" in summary
    assert mock_memories.retrieve.called
    call_kwargs = mock_memories.retrieve.call_args.kwargs
    assert call_kwargs["name"] == DEFAULT_MEMORY_BANK_ENGINE
    assert call_kwargs["scope"] == {"user_id": "cust_sdk_123"}
    assert call_kwargs["similarity_search_params"] == {"search_query": "order update", "top_k": 5}

    # 2. Test generate_turn_memories
    client.generate_turn_memories(
        customer_id="cust_sdk_123",
        user_message_tokenized="Order update for [ORDER_0]",
        decision_summary="Decision: auto_rag, queue: none",
        async_mode=False,
    )
    assert mock_memories.generate.called
    gen_kwargs = mock_memories.generate.call_args.kwargs
    assert gen_kwargs["name"] == DEFAULT_MEMORY_BANK_ENGINE
    assert gen_kwargs["scope"] == {"user_id": "cust_sdk_123"}
    assert gen_kwargs["direct_contents_source"] is not None
    assert gen_kwargs["config"].revision_ttl == "15552000s"
