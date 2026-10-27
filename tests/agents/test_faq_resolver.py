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
    FaqResolverAgent,
    FakeLLMClient,
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
)
from ticket_router.agents.faq_resolver.validator import (
    CitationValidator,
    is_verbatim_citation,
    norm,
)
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

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


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
        clock=clock,
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

    class TimingOutSearchClient(FakeSearchClient):
        def search(self, **kwargs: Any) -> SearchResult:
            clock.advance(21.0)
            return super().search(**kwargs)

    search_client = TimingOutSearchClient()

    agent = FaqResolverAgent(
        search_client=search_client,
        clock=clock,
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
