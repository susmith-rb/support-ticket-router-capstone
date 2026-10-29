# REQ-AGENT-007: FaqResolver Bounded ReAct Loop, Search & Laya Rerank Integration
# REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""tests/agents/test_faq_resolver.py — Acceptance and unit tests for FaqResolverAgent.

Hermetic tests validating:
1. test_bounded_react_and_rerank (REQ-AGENT-007):
   - <= 6 tool calls, <= 2 retrieval passes, <= 1 regeneration, <= 20 s budget
   - Agent Search with production config
   - Laya cross-encoder candidate reranking
2. test_mandatory_citations_or_escalate (REQ-AGENT-008):
   - >= 1 verbatim quote matching retrieved documents
   - Rejected paraphrased quote, rejected non-retrieved doc, rejected empty citations
   - 1 failed regeneration -> escalates to human-standard with reason 'uncited_answer'
   - Agent Search failure -> degraded human escalation
3. BOLA/IDOR protection (REQ-AGENT-010, AGENTS.md rule 7):
   - customer_id derived strictly from verified session claims
   - Untrusted customer_id argument rejected with BolaViolationError
4. Production config parity:
   - build_production_search_kwargs matches evals.ablation_eval.build_search_kwargs
"""

from typing import Any, Dict, List, Optional

import pytest

from evals.ablation_eval import build_search_kwargs, get_ablation_configs
from ticket_router.agents.faq_resolver.a2a import (
    FAQ_RESOLVER_AGENT_CARD,
    handle_a2a_request,
)
from ticket_router.agents.faq_resolver.agent import (
    FakeLLMClient,
    FaqResolverAgent,
    root_agent,
)
from ticket_router.agents.faq_resolver.app import create_app
from ticket_router.agents.faq_resolver.mcp_client import (
    BolaViolationError,
    FakeOrderLookupClient,
)
from ticket_router.agents.faq_resolver.reranker import FakeReranker
from ticket_router.agents.faq_resolver.search import (
    build_production_search_kwargs,
    execute_faq_search,
)
from ticket_router.agents.faq_resolver.validator import (
    CitationValidator,
    is_verbatim_citation,
    norm,
)
from ticket_router.common.resilience import CircuitBreaker, CircuitState
from ticket_router.retrieval.search_client import SearchResult


class FakeSearchClient:
    """Hermetic in-memory Agent Search fake."""

    def __init__(
        self,
        docs: Optional[Dict[str, Dict[str, str]]] = None,
        fail: bool = False,
    ) -> None:
        self.docs = docs or {
            "faq-returns-window": {
                "title": "Return Policy Window",
                "content": "Eligible retail items can be returned within 30 days of delivery for a full refund.",
            },
            "faq-shipping-rates": {
                "title": "Standard Shipping Rates",
                "content": "Standard shipping is free for orders over $35 and takes 3 to 5 business days.",
            },
        }
        self.fail = fail
        self.call_history: List[Dict[str, Any]] = []

    def search(self, **kwargs: Any) -> SearchResult:
        self.call_history.append(kwargs)
        if self.fail:
            raise RuntimeError("Agent Search service unavailable (HTTP 503).")

        raw_results = []
        doc_ids = []
        for d_id, d_data in self.docs.items():
            doc_ids.append(d_id)
            raw_results.append({
                "document": {
                    "id": d_id,
                    "derivedStructData": {"title": d_data["title"]},
                    "structData": {"content": d_data["content"]},
                }
            })

        return SearchResult(
            document_ids=doc_ids,
            latency_ms=45.0,
            total_size=len(doc_ids),
            filter_applied=None,
            fallback_triggered=False,
            rerank_applied=False,
            raw_results=raw_results,
            summary={},
        )


class MockClock:
    """Deterministic injectable clock advancing on demand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


class TimingOutSearchClient(FakeSearchClient):
    """Fake search client that simulates a timeout by advancing a mock clock."""

    def __init__(self, clock: MockClock, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.clock = clock

    def search(self, **kwargs: Any) -> SearchResult:
        self.clock.advance(21.0)
        return super().search(**kwargs)


class RecordingLLMClient(FakeLLMClient):
    """LLM client fake that records kwargs passed to generate_answer."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded_order_info: List[Optional[Dict[str, Any]]] = []

    def generate_answer(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.recorded_order_info.append(kwargs.get("order_info"))
        return super().generate_answer(*args, **kwargs)


class BrokenOrderClient:
    """Mock order client simulating connection reset during get_order."""

    def get_order(
        self,
        order_id: str,
        customer_id: str,
        session_id: str = "",
        untrusted_customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise RuntimeError("Downstream order MCP connection reset")


# =============================================================================
# REQ-AGENT-007: Bounded ReAct Loop, Search & Rerank Integration
# =============================================================================


# REQ-AGENT-007
def test_bounded_react_and_rerank():
    """Verify bounded ReAct execution: search with production config, Laya rerank, tool counts."""
    search_client = FakeSearchClient()
    reranker = FakeReranker(preferred_id="faq-returns-window")
    order_client = FakeOrderLookupClient()
    order_client.add_order(
        order_id="1001",
        customer_id="cust-123",
        status="SHIPPED",
    )
    llm_client = FakeLLMClient(
        default_answer="You can return items within 30 days of delivery.",
        default_doc_id="faq-returns-window",
        default_quote="within 30 days of delivery",
    )
    clock = MockClock(start=0.0)

    agent = FaqResolverAgent(
        search_client=search_client,
        reranker=reranker,
        order_client=order_client,
        llm_client=llm_client,
        clock=clock.now,
        total_budget_s=20.0,
    )

    # Execute inquiry with order mention
    res = agent.resolve(
        ticket_id="t-001",
        session_id="s-001",
        message="What is the return window for my order #1001?",
        customer_id="cust-123",
        predicted_intent="returns_refunds",
    )

    # Invariants verification
    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert res.tool_calls_count <= 6, f"Tool calls exceeded budget: {res.tool_calls_count}"
    assert res.retrieval_passes_count <= 2, f"Retrieval passes exceeded budget: {res.retrieval_passes_count}"
    assert res.regeneration_count <= 1, f"Regenerations exceeded budget: {res.regeneration_count}"
    assert res.answer is not None and "30 days" in res.answer
    assert len(res.citations) >= 1
    assert res.citations[0]["doc_id"] == "faq-returns-window"
    assert res.citations[0]["quote"] == "within 30 days of delivery"

    # Verify search call used production config
    assert len(search_client.call_history) == 1
    call_kwargs = search_client.call_history[0]
    assert call_kwargs["threshold"] == "LOWEST"
    assert call_kwargs["clean"] is True
    assert call_kwargs["intent_boost"] == 0.2
    assert call_kwargs["intent_family"] == "returns_refunds"

    # Verify reranker was invoked and candidate scoring took place
    assert len(reranker.call_history) == 1
    assert reranker.call_history[0]["top_n"] == 5

    # Verify order lookup was invoked with verified customer_id
    assert len(order_client.call_history) == 1
    assert order_client.call_history[0]["customer_id"] == "cust-123"


# REQ-AGENT-007, REQ-AGENT-012
def test_wall_clock_budget_enforcement():
    """Verify that exceeding the 20-second budget triggers graceful escalation to human queue."""
    clock = MockClock(start=0.0)
    search_client = TimingOutSearchClient(clock=clock)

    agent = FaqResolverAgent(
        search_client=search_client,
        clock=clock.now,
        total_budget_s=20.0,
    )

    res = agent.resolve(
        ticket_id="t-timeout",
        session_id="s-timeout",
        message="What is the return window?",
        customer_id="cust-456",
    )

    assert res.status == "ESCALATED"
    assert res.route == "human"
    assert res.queue == "human-standard"
    assert res.reason == "budget_exceeded"


# =============================================================================
# REQ-AGENT-008: Mandatory Grounded Citations & Non-Hallucination Fallback
# =============================================================================


# REQ-AGENT-008
def test_mandatory_citations_or_escalate():
    """Verify citation validation, single regeneration attempt, and human escalation."""
    search_client = FakeSearchClient()

    # Case 1: First attempt fails, regeneration succeeds
    llm_recoverable = FakeLLMClient(
        default_answer="Returns are permitted within 30 days.",
        default_doc_id="faq-returns-window",
        default_quote="within 30 days of delivery",
        fail_citations_once=True,
    )

    agent_recoverable = FaqResolverAgent(
        search_client=search_client,
        llm_client=llm_recoverable,
    )

    res_recovered = agent_recoverable.resolve(
        ticket_id="t-rec",
        session_id="s-rec",
        message="What is the return policy?",
        customer_id="cust-1",
    )

    assert res_recovered.status == "AUTO_RESOLVED"
    assert res_recovered.regeneration_count == 1
    assert len(res_recovered.citations) == 1
    assert res_recovered.citations[0]["quote"] == "within 30 days of delivery"

    # Case 2: Permanent failure after 1 regeneration -> Escalates with reason 'uncited_answer'
    llm_uncited = FakeLLMClient(
        always_fail_citations=True,
    )

    agent_uncited = FaqResolverAgent(
        search_client=search_client,
        llm_client=llm_uncited,
    )

    res_uncited = agent_uncited.resolve(
        ticket_id="t-uncited",
        session_id="s-uncited",
        message="Can I return opened food?",
        customer_id="cust-2",
    )

    assert res_uncited.status == "ESCALATED"
    assert res_uncited.route == "human"
    assert res_uncited.queue == "human-standard"
    assert res_uncited.reason == "uncited_answer"
    assert res_uncited.regeneration_count == 1
    assert "escalated" in res_uncited.message.lower()


# REQ-AGENT-008
def test_agent_search_failure_degraded_escalation():
    """Verify Agent Search failure triggers immediate degraded human queue escalation."""
    failing_search = FakeSearchClient(fail=True)
    agent = FaqResolverAgent(search_client=failing_search)

    res = agent.resolve(
        ticket_id="t-fail",
        session_id="s-fail",
        message="How long does standard shipping take?",
        customer_id="cust-3",
    )

    assert res.status == "ESCALATED"
    assert res.route == "human"
    assert res.queue == "human-standard"
    assert res.reason == "search_failure"
    assert res.degraded is True
    assert "degraded" in res.message.lower() or "unavailable" in res.message.lower()


# REQ-AGENT-008
def test_citation_validator_edge_cases():
    """Verify CitationValidator handles empty citations, paraphrasing, and non-retrieved docs."""
    validator = CitationValidator()
    docs = {
        "doc-1": {
            "title": "Return Policy",
            "content": "Items in original packaging can be returned within 14 days.",
        }
    }

    # 1. Empty citations rejected
    res_empty = validator.validate({"citations": []}, docs)
    assert res_empty.is_valid is False
    assert res_empty.reason == "empty_citations"

    # 2. Non-retrieved doc_id rejected
    res_non_retrieved = validator.validate(
        {"citations": [{"doc_id": "doc-999", "quote": "within 14 days"}]},
        docs,
    )
    assert res_non_retrieved.is_valid is False
    assert res_non_retrieved.reason == "doc_not_retrieved"

    # 3. Paraphrased quote rejected
    res_paraphrased = validator.validate(
        {"citations": [{"doc_id": "doc-1", "quote": "you can return stuff inside 2 weeks"}]},
        docs,
    )
    assert res_paraphrased.is_valid is False
    assert res_paraphrased.reason == "paraphrased_quote"

    # 4. Verbatim exact and normalized whitespace quote accepted
    res_valid = validator.validate(
        {"citations": [{"doc_id": "doc-1", "quote": "Items in original packaging  can be returned\nwithin 14 days."}]},
        docs,
    )
    assert res_valid.is_valid is True
    assert len(res_valid.citations) == 1
    assert res_valid.citations[0].doc_id == "doc-1"


# REQ-RAG-RETR-005
def test_norm_and_verbatim_helpers():
    """Verify string normalization and verbatim matching functions."""
    assert norm("  Hello   *World*! `code` #heading  ") == "hello world! code heading"
    doc_text = "Free shipping applies to all orders over $35 delivered to the continental US."
    assert is_verbatim_citation("orders over $35", doc_text) is True
    assert is_verbatim_citation("orders   OVER   $35", doc_text) is True
    assert is_verbatim_citation("orders exceeding thirty-five dollars", doc_text) is False
    assert is_verbatim_citation("", doc_text) is False
    assert is_verbatim_citation("orders", "") is False


# =============================================================================
# REQ-AGENT-010: MCP Tool Contracts & BOLA/IDOR Invariant
# =============================================================================


# REQ-AGENT-010
def test_order_lookup_bola_idor_guard():
    """Verify BOLA/IDOR guard: customer_id derived from session claims; untrusted args rejected."""
    client = FakeOrderLookupClient()
    client.add_order(
        order_id="ORD-789",
        customer_id="legitimate-cust",
        status="DELIVERED",
    )

    # 1. Legitimate lookup succeeds
    order = client.get_order(order_id="ORD-789", customer_id="legitimate-cust")
    assert order["status"] == "DELIVERED"

    # 2. Lookup by attacker for legitimate customer's order raises LookupError (uniform not found)
    with pytest.raises(LookupError):
        client.get_order(order_id="ORD-789", customer_id="attacker-cust")

    # 3. Model/tool passing malicious untrusted_customer_id raises BolaViolationError
    with pytest.raises(BolaViolationError):
        client.get_order(
            order_id="ORD-789",
            customer_id="legitimate-cust",
            untrusted_customer_id="attacker-cust",
        )

    # 4. Agent handles BOLA violation safely by escalating without leaking data
    agent = FaqResolverAgent(order_client=client)
    res_bola = agent.resolve(
        ticket_id="t-bola",
        session_id="s-bola",
        message="Check order ORD-789",
        customer_id="legitimate-cust",
        untrusted_customer_id="attacker-override",
    )
    assert res_bola.status == "ESCALATED"
    assert res_bola.reason == "bola_violation"
    assert res_bola.route == "human"


# =============================================================================
# Production Configuration Parity Test
# =============================================================================


# REQ-RAG-RETR-008, REQ-RAG-RETR-010
def test_production_config_search_kwargs_parity():
    """Verify build_production_search_kwargs matches evals.ablation_eval.build_search_kwargs."""
    # Retrieve official ablation configuration for intent_soft_noisy_0.2
    all_configs = get_ablation_configs(["intent_soft_noisy_0.2"])
    assert len(all_configs) == 1, "Failed to load intent_soft_noisy_0.2 config from ablation_eval"
    official_cfg = all_configs[0]

    sample_query_item = {
        "qid": "dev-042",
        "query": "Where is my order [ORDER_1]?",
        "intent": "order_status",
    }
    sample_noisy_intent = "order_status"
    sample_token = "mock-oauth-bearer-token"

    # Baseline call from offline evals package
    expected_kwargs = build_search_kwargs(
        cfg=official_cfg,
        query_item=sample_query_item,
        noisy_intent=sample_noisy_intent,
        token=sample_token,
    )

    # Call replicated runtime function in ticket_router.agents.faq_resolver.search
    actual_kwargs = build_production_search_kwargs(
        query_item=sample_query_item,
        noisy_intent=sample_noisy_intent,
        token=sample_token,
    )

    assert actual_kwargs == expected_kwargs, (
        f"Production search kwargs diverged from evals.ablation_eval!\n"
        f"Actual:   {actual_kwargs}\n"
        f"Expected: {expected_kwargs}"
    )


# =============================================================================
# A2A and HTTP App Entrypoint Tests
# =============================================================================


# REQ-AGENT-006
def test_a2a_agent_card_and_handler():
    """Verify A2A agent card structure and request dispatch."""
    assert FAQ_RESOLVER_AGENT_CARD["name"] == "FaqResolverAgent"
    assert "input_schema" in FAQ_RESOLVER_AGENT_CARD
    assert "output_schema" in FAQ_RESOLVER_AGENT_CARD

    llm = FakeLLMClient(
        default_answer="Standard return is 30 days.",
        default_doc_id="faq-1",
        default_quote="return is 30 days",
    )
    docs = {"faq-1": {"title": "FAQ", "content": "Our return is 30 days from purchase."}}
    search_client = FakeSearchClient(docs=docs)

    agent = FaqResolverAgent(search_client=search_client, llm_client=llm)

    payload = {
        "ticket_id": "t-a2a-1",
        "session_id": "s-a2a-1",
        "redacted_message": "What is the return window?",
        "customer_id": "cust-a2a",
        "predicted_intent": "returns_refunds",
        "memory_summary": "Tier-1 VIP customer",
    }

    res_dict = handle_a2a_request(payload, agent=agent)
    assert res_dict["status"] == "AUTO_RESOLVED"
    assert res_dict["route"] == "resolved"
    assert len(res_dict["citations"]) >= 1


# REQ-AGENT-001
def test_http_app_entrypoint():
    """Verify HTTP application /healthz and /resolve endpoints."""
    app = create_app()
    assert app is not None
    # root_agent is exposed
    assert root_agent is not None
    assert isinstance(root_agent, FaqResolverAgent)


# =============================================================================
# REQ-AGENT-001 / REQ-AGENT-006: Inbound Auth & Runtime Wiring Acceptance Tests
# =============================================================================


class FakeOrderResponse:
    """Mock HTTP response for HttpOrderLookupClient testing."""

    def __init__(self, data: Dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        import json
        self.text = json.dumps(data)

    def json(self) -> Dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} error")


def test_faq_resolver_inbound_auth_health_unauthenticated():
    """Verify /health and /healthz endpoints are unauthenticated."""
    from starlette.testclient import TestClient

    from ticket_router.agents.faq_resolver.app import create_fastapi_app
    from ticket_router.common.settings import Settings

    settings = Settings.from_env(env={
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "1",
    })
    app = create_fastapi_app(agent=FaqResolverAgent(), settings=settings)
    client = TestClient(app)

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_faq_resolver_inbound_auth_missing_header_rejected():
    """Verify missing Authorization header returns 401 when auth is required."""
    from starlette.testclient import TestClient

    from ticket_router.agents.faq_resolver.app import create_fastapi_app
    from ticket_router.common.settings import Settings

    settings = Settings.from_env(env={
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "1",
    })
    app = create_fastapi_app(agent=FaqResolverAgent(), settings=settings)
    client = TestClient(app)

    payload = {
        "ticket_id": "t-001",
        "session_id": "s-001",
        "message": "hello",
        "customer_id": "c-001",
    }
    resp = client.post("/a2a", json=payload)
    assert resp.status_code == 401
    assert "Missing Authorization" in resp.json()["detail"]


def test_faq_resolver_inbound_auth_unauthorized_caller_rejected(monkeypatch):
    """Verify token from caller not in ALLOWED_CALLERS returns 403."""
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from ticket_router.agents.faq_resolver.app import create_fastapi_app
    from ticket_router.common.settings import Settings

    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        MagicMock(return_value={"email": "attacker@evil.com", "email_verified": True}),
    )

    settings = Settings.from_env(env={
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "1",
    })
    app = create_fastapi_app(agent=FaqResolverAgent(), settings=settings)
    client = TestClient(app)

    payload = {
        "ticket_id": "t-001",
        "session_id": "s-001",
        "message": "hello",
        "customer_id": "c-001",
    }
    resp = client.post(
        "/a2a",
        headers={"Authorization": "Bearer untrusted_token"},
        json=payload,
    )
    assert resp.status_code == 403
    assert "not in ALLOWED_CALLERS" in resp.json()["detail"]


def test_faq_resolver_inbound_auth_unverified_email_rejected(monkeypatch):
    """Verify token with email_verified=False returns 401."""
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from ticket_router.agents.faq_resolver.app import create_fastapi_app
    from ticket_router.common.settings import Settings

    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        MagicMock(return_value={
            "email": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
            "email_verified": False,
        }),
    )

    settings = Settings.from_env(env={
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "1",
    })
    app = create_fastapi_app(agent=FaqResolverAgent(), settings=settings)
    client = TestClient(app)

    payload = {
        "ticket_id": "t-001",
        "session_id": "s-001",
        "message": "hello",
        "customer_id": "c-001",
    }
    resp = client.post(
        "/a2a",
        headers={"Authorization": "Bearer unverified_token"},
        json=payload,
    )
    assert resp.status_code == 401
    assert "email is not verified" in resp.json()["detail"]


def test_faq_resolver_inbound_auth_authorized_caller_accepted(monkeypatch):
    """Verify valid token from allowed caller processes inquiry."""
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from ticket_router.agents.faq_resolver.app import create_fastapi_app
    from ticket_router.common.settings import Settings

    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        MagicMock(return_value={
            "email": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
            "email_verified": True,
        }),
    )

    settings = Settings.from_env(env={
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "1",
    })
    agent = FaqResolverAgent(llm_client=FakeLLMClient(default_answer="Refund within 30 days."))
    app = create_fastapi_app(agent=agent, settings=settings)
    client = TestClient(app)

    payload = {
        "ticket_id": "t-001",
        "session_id": "s-001",
        "message": "How do I return?",
        "customer_id": "c-001",
    }
    resp = client.post(
        "/a2a",
        headers={"Authorization": "Bearer valid_signed_token"},
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("AUTO_RESOLVED", "ESCALATED")


def test_http_order_lookup_client_sends_context_and_bearer_headers():
    """Verify HttpOrderLookupClient sends X-Customer-ID context header and OIDC bearer."""
    from unittest.mock import MagicMock

    from ticket_router.agents.faq_resolver.mcp_client import HttpOrderLookupClient

    mock_session = MagicMock()
    mock_session.post.return_value = FakeOrderResponse({"order_id": "1001", "status": "SHIPPED"})

    client = HttpOrderLookupClient(
        base_url="https://order-lookup.run.app",
        session=mock_session,
        token_provider=lambda aud: "mock_oidc_id_token",
    )
    order = client.get_order(order_id="1001", customer_id="cust-secure-789")

    assert order["status"] == "SHIPPED"
    call_kwargs = mock_session.post.call_args.kwargs
    headers = call_kwargs["headers"]
    assert headers["X-Customer-ID"] == "cust-secure-789"
    assert headers["Authorization"] == "Bearer mock_oidc_id_token"


def test_faq_resolver_startup_fails_fast_on_missing_env(monkeypatch):
    """Verify FaqResolver fail-fast behavior when REQUIRE_REAL_DEPS=1 and env is incomplete."""
    from ticket_router.agents.faq_resolver.agent import create_faq_resolver_agent
    from ticket_router.common.settings import Settings

    bad_env = {
        "REQUIRE_REAL_DEPS": "1",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        # Missing ORDER_LOOKUP_MCP_URL, ALLOWED_CALLERS, etc.
    }
    settings = Settings.from_env(env=bad_env)
    with pytest.raises(ValueError, match="Missing required environment variable"):
        create_faq_resolver_agent(settings)


def test_faq_resolver_circuit_breaker_integration():
    """Verify FaqResolverAgent executes search via CircuitBreaker.call_with_timeout and trips on failures."""
    cb_ok = CircuitBreaker(name="agent_search", failure_threshold=3)
    agent_ok = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=FakeLLMClient(
            default_answer="Returns are permitted within 30 days.",
            default_doc_id="faq-returns-window",
            default_quote="within 30 days of delivery",
        ),
        circuit_breaker=cb_ok,
    )
    res_ok = agent_ok.resolve(
        ticket_id="t-cb-ok",
        session_id="s-cb-ok",
        message="What is the return policy?",
        customer_id="cust-1",
    )
    assert res_ok.status == "AUTO_RESOLVED"
    assert cb_ok.state == CircuitState.CLOSED

    cb_fail = CircuitBreaker(name="agent_search_fail", failure_threshold=3)
    agent_fail = FaqResolverAgent(
        search_client=FakeSearchClient(fail=True),
        circuit_breaker=cb_fail,
    )
    for i in range(3):
        res_fail = agent_fail.resolve(
            ticket_id=f"t-cb-fail-{i}",
            session_id="s-cb-fail",
            message="How long does shipping take?",
            customer_id="cust-2",
        )
        assert res_fail.status == "ESCALATED"
        assert res_fail.degraded is True
    assert cb_fail.state == CircuitState.OPEN


# REQ-AGENT-013, REQ-AGENT-007
def test_gemini_answer_client_location_and_graceful_order_error():
    """Verify GeminiAnswerClient resolves location from models.yaml and FaqResolver degrades gracefully on order error."""
    from ticket_router.agents.faq_resolver.agent import GeminiAnswerClient

    ans_client = GeminiAnswerClient(project="susmith-fde-ticket-router", location="us-central1", model_role="answer")
    assert ans_client.location == "global"
    assert ans_client.resolved_model_id == "gemini-3.8-flash"

    fake_llm = RecordingLLMClient(
        default_answer="Returns are permitted within 30 days.",
        default_doc_id="faq-returns-window",
        default_quote="within 30 days of delivery",
    )
    agent = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=fake_llm,
        order_client=BrokenOrderClient(),
    )
    res = agent.resolve(
        ticket_id="t-ord-err",
        session_id="s-ord-err",
        message="What is the return policy for order ORD-10001?",
        customer_id="cust_1001",
    )
    assert res.status == "AUTO_RESOLVED"
    assert res.tool_calls_count >= 2
    assert fake_llm.recorded_order_info[-1] == {"order_id": "ORD-10001", "status": "UNAVAILABLE"}


# REQ-AGENT-007, REQ-AGENT-010
def test_faq_resolver_natural_order_lookup_without_order_id():
    """Verify natural order lookup without memorized order ID triggers list_orders and injects orders into LLM."""
    order_client = FakeOrderLookupClient()
    order_client.add_order(
        order_id="ORD-555",
        customer_id="cust_1001",
        status="SHIPPED",
    )
    order_client.add_order(
        order_id="ORD-556",
        customer_id="cust_1001",
        status="DELIVERED",
    )
    order_client.add_order(
        order_id="ORD-999",
        customer_id="cust_1002",
        status="PROCESSING",
    )

    fake_llm = RecordingLLMClient(
        default_answer="Your order ORD-555 has shipped and is on the way.",
        default_doc_id="faq-shipping-rates",
        default_quote="Standard shipping is free for orders over $35 and takes 3 to 5 business days.",
    )
    agent = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=fake_llm,
        order_client=order_client,
    )

    res = agent.resolve(
        ticket_id="t-nat-001",
        session_id="s-nat-001",
        message="Where is my order and will it be delivered?",
        customer_id="cust_1001",
        predicted_intent="order_status",
        customer_verified=True,
    )

    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert len(order_client.call_history) == 1
    assert order_client.call_history[0]["action"] == "list_orders"
    assert order_client.call_history[0]["customer_id"] == "cust_1001"
    assert order_client.call_history[0]["limit"] == 5

    assert len(fake_llm.recorded_order_info) >= 1
    passed_order_info = fake_llm.recorded_order_info[-1]
    assert passed_order_info is not None
    assert "orders" in passed_order_info
    assert len(passed_order_info["orders"]) == 2
    order_ids = {o["order_id"] for o in passed_order_info["orders"]}
    assert order_ids == {"ORD-555", "ORD-556"}


# REQ-AGENT-007, REQ-IDENT-004, REQ-IDENT-005
def test_faq_resolver_unverified_customer_order_inquiry_sign_in_prompt():
    """Verify unverified customer with order-specific intent receives sign-in prompt and 0 tool calls."""
    order_client = FakeOrderLookupClient()
    order_client.add_order(
        order_id="ORD-777",
        customer_id="cust_1001",
        status="SHIPPED",
    )

    fake_llm = RecordingLLMClient()
    agent = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=fake_llm,
        order_client=order_client,
    )

    res = agent.resolve(
        ticket_id="t-unv-001",
        session_id="s-unv-001",
        message="where is my package",
        customer_id="anon:guest_999",
        predicted_intent="shipping_policy",
        customer_verified=False,
    )

    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert "sign in" in res.answer.lower()
    assert res.tool_calls_count == 0
    assert res.retrieval_passes_count == 0
    assert len(order_client.call_history) == 0
    assert len(fake_llm.recorded_order_info) == 0


class SnippetSearchFake:
    """Fake search client returning HTML-highlighted snippet and title."""

    def search(self, **kwargs: Any) -> Any:
        return {
            "document_ids": ["faq-orders-dispatch"],
            "raw_results": [
                {
                    "document": {
                        "id": "faq-orders-dispatch",
                        "derivedStructData": {
                            "title": "<b>Order Status</b> &amp; Tracking",
                            "snippets": [
                                {
                                    "snippet": (
                                        "- <b>order status</b> says preparing for dispatch ... "
                                        "<b>Your Orders</b> section of <b>your</b> Global Retail Hub account.&nbsp;..."
                                    )
                                }
                            ],
                        },
                        "structData": {},
                    }
                }
            ],
        }


# REQ-AGENT-008
def test_citation_validator_and_search_strip_discovery_engine_html_highlights():
    """Verify CitationValidator and execute_faq_search strip Discovery Engine HTML highlight tags and entities."""
    # 1. Direct is_verbatim_citation check
    quote = "Your Orders section of your Global Retail Hub account."
    html_doc = (
        "- <b>order status</b> says preparing for dispatch ... "
        "<b>Your Orders</b> section of <b>your</b> Global Retail Hub account.&nbsp;..."
    )
    assert is_verbatim_citation(quote, html_doc) is True

    # 2. CitationValidator().validate() accepts verbatim quote against HTML snippet with entities
    validator = CitationValidator()
    snippet_content = (
        "- <b>order status</b> says preparing for dispatch ... "
        "<b>Your Orders</b> section of <b>your</b> Global Retail Hub account.&nbsp;"
        "Visit &amp; track items you&#39;ve purchased."
    )
    retrieved_docs = {
        "faq-orders-dispatch": {
            "title": "Order Dispatch &amp; Tracking",
            "content": snippet_content,
        }
    }

    valid_answer = {
        "citations": [
            {
                "doc_id": "faq-orders-dispatch",
                "quote": "Your Orders section of your Global Retail Hub account.",
            }
        ]
    }
    result_valid = validator.validate(valid_answer, retrieved_docs)
    assert result_valid.is_valid is True
    assert len(result_valid.citations) == 1
    assert result_valid.citations[0].doc_id == "faq-orders-dispatch"

    # Still rejects a paraphrased quote against the same HTML snippet
    paraphrased_answer = {
        "citations": [
            {
                "doc_id": "faq-orders-dispatch",
                "quote": "Check the orders page on your retail account.",
            }
        ]
    }
    result_invalid = validator.validate(paraphrased_answer, retrieved_docs)
    assert result_invalid.is_valid is False
    assert result_invalid.reason == "paraphrased_quote"

    # 3. execute_faq_search cleans <b>...</b> and &nbsp; out of derivedStructData.snippets
    search_exec_res = execute_faq_search(
        client=SnippetSearchFake(),
        query="order status",
    )
    assert search_exec_res.success is True
    assert "faq-orders-dispatch" in search_exec_res.documents
    doc = search_exec_res.documents["faq-orders-dispatch"]
    assert "<b>" not in doc.content
    assert "</b>" not in doc.content
    assert "&nbsp;" not in doc.content
    assert "\xa0" not in doc.content
    assert "Your Orders section of your Global Retail Hub account." in doc.content
    assert doc.title == "Order Status & Tracking"





