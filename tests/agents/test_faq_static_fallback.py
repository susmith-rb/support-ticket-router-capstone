"""tests/agents/test_faq_static_fallback.py — Acceptance and unit tests for REQ-AGENT-028 and REQ-AGENT-029.

# REQ-AGENT-028: Grounded Static-Passage Fallback When Answer Generation Stalls
# REQ-AGENT-029: Earlier Answer Hedge and Budget-Aware Citation Regeneration
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from ticket_router.agents.faq_resolver.agent import (
    FakeLLMClient,
    FaqResolverAgent,
)
from ticket_router.agents.faq_resolver.passage_selector import (
    build_static_fallback_payload,
    select_static_passage,
)
from ticket_router.agents.faq_resolver.validator import CitationValidator
from ticket_router.common.resilience import (
    ANSWER_DEADLINE_S,
    ANSWER_HEDGE_AFTER_S,
    FAQ_OVERALL_BUDGET_S,
    ServiceTimeoutError,
)
from ticket_router.retrieval.search_client import SearchResult
from ticket_router.security.model_armor import ModelArmorVerdict


class FakeSearchClient:
    """Hermetic search fake returning controlled documents with optional score."""

    def __init__(
        self,
        docs: Optional[Dict[str, Dict[str, Any]]] = None,
        fail: bool = False,
    ) -> None:
        self.docs = docs or {
            "faq-returns-window": {
                "title": "Return Policy Window",
                "content": (
                    "Global Retail Hub offers clear rules on all retail purchases. "
                    "Under our standard return policy window, eligible retail items can be returned within 30 days of delivery for a full refund. "
                    "Items must be in original condition with tags attached. "
                    "Return shipping is free for store credit."
                ),
                "score": 0.95,
            }
        }
        self.fail = fail

    def search(self, **kwargs: Any) -> SearchResult:
        if self.fail:
            raise RuntimeError("Agent Search unavailable")
        raw_results = []
        doc_ids = []
        for d_id, d_data in self.docs.items():
            doc_ids.append(d_id)
            raw_results.append({
                "document": {
                    "id": d_id,
                    "derivedStructData": {"title": d_data.get("title", d_id)},
                    "structData": {
                        "content": d_data.get("content", ""),
                    },
                    "score": d_data.get("score"),
                },
                "score": d_data.get("score"),
            })
        return SearchResult(
            document_ids=doc_ids,
            latency_ms=25.0,
            total_size=len(doc_ids),
            filter_applied=None,
            fallback_triggered=False,
            rerank_applied=False,
            raw_results=raw_results,
            summary={},
        )


class TimeoutLLMClient:
    """Fake LLM client that raises ServiceTimeoutError or other exceptions."""

    def __init__(self, exc: Optional[Exception] = None) -> None:
        self.calls = 0
        self.exc = exc or ServiceTimeoutError("Hedged call exceeded deadline of 15000.0 ms")

    def generate_answer(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        raise self.exc


class ControllableLLMClient(FakeLLMClient):
    """Fake LLM client that records all kwargs and can return uncited on call 1."""

    def __init__(self, fail_first: bool = False, fail_regen_with_timeout: bool = False) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.fail_regen_with_timeout = fail_regen_with_timeout
        self.call_records: List[Dict[str, Any]] = []

    def generate_answer(self, **kwargs: Any) -> Dict[str, Any]:
        self.call_records.append(dict(kwargs))
        call_num = len(self.call_records)
        if self.fail_first and call_num == 1:
            return {
                "answer": "You can return items anytime you want.",
                "citations": [{"doc_id": "non_existent", "quote": "fake quote"}],
            }
        if self.fail_regen_with_timeout and call_num == 2:
            raise ServiceTimeoutError("Regeneration timed out after deadline")
        return super().generate_answer(**kwargs)


class FakeClock:
    """Deterministic monotonic clock for testing execution budgets."""

    def __init__(self, initial_time: float = 100.0) -> None:
        self.current_time = initial_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


# REQ-AGENT-028
def test_static_fallback_on_generation_timeout():
    """Verify static grounded fallback when Gemini generation times out with docs present.

    # REQ-AGENT-028: Grounded Static-Passage Fallback When Answer Generation Stalls.
    Must return status AUTO_RESOLVED, degraded True, reason 'static_fallback',
    with exact citation quote and passing the citation validator.
    """
    timeout_client = TimeoutLLMClient()
    search_client = FakeSearchClient()
    faq_agent = FaqResolverAgent(
        search_client=search_client,
        llm_client=timeout_client,  # type: ignore[arg-type]
        reranker=None,
    )

    res = faq_agent.resolve(
        ticket_id="TICK-STATIC-001",
        session_id="sess-static-001",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )

    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert res.degraded is True
    assert res.reason == "static_fallback"
    assert res.answer is not None
    assert "Return Policy Window" in res.answer
    assert "within 30 days of delivery for a full refund" in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["doc_id"] == "faq-returns-window"
    assert "within 30 days of delivery for a full refund" in res.citations[0]["quote"]
    assert timeout_client.calls == 1

    # Verify validator acceptance on the returned result
    validator = CitationValidator()
    docs_map = {
        "faq-returns-window": {
            "title": "Return Policy Window",
            "content": search_client.docs["faq-returns-window"]["content"],
        }
    }
    val = validator.validate(res.to_dict(), docs_map)
    assert val.is_valid is True
    assert len(val.citations) == 1


# REQ-AGENT-029
def test_regeneration_respects_remaining_budget():
    """Verify regeneration checks remaining budget and caps per-call deadline.

    # REQ-AGENT-029: Earlier Answer Hedge and Budget-Aware Citation Regeneration.
    1. Skip regeneration if remaining budget < ANSWER_HEDGE_AFTER_S + 2.0 s (6.0 s)
       and use static fallback.
    2. Run regeneration if remaining budget >= 6.0 s, capping deadline to
       min(ANSWER_DEADLINE_S, remaining - 1.0 s).
    """
    clock = FakeClock(100.0)

    # Sub-case 1: Remaining budget = 5.0 s < 6.0 s -> regeneration skipped!
    # Turn starts at 100.0, total budget = 20.0 (ends at 120.0)
    llm_client_1 = ControllableLLMClient(fail_first=True)
    faq_agent_1 = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=llm_client_1,
        clock=clock,
        total_budget_s=FAQ_OVERALL_BUDGET_S,
    )

    orig_generate_1 = llm_client_1.generate_answer

    def timed_generate_1(**kwargs: Any) -> Dict[str, Any]:
        clock.advance(15.0)  # Consumes 15.0 seconds
        return orig_generate_1(**kwargs)

    llm_client_1.generate_answer = timed_generate_1  # type: ignore[method-assign]

    res_1 = faq_agent_1.resolve(
        ticket_id="TICK-BUDGET-SKIP",
        session_id="sess-budget-skip",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )

    assert len(llm_client_1.call_records) == 1  # Regeneration was skipped!
    assert res_1.status == "AUTO_RESOLVED"
    assert res_1.reason == "static_fallback"
    assert res_1.degraded is True
    assert res_1.regeneration_count == 0

    # Sub-case 2: Remaining budget = 10.0 s >= 6.0 s -> regeneration runs with capped deadline!
    clock_2 = FakeClock(200.0)
    llm_client_2 = ControllableLLMClient(fail_first=True)
    faq_agent_2 = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=llm_client_2,
        clock=clock_2,
        total_budget_s=FAQ_OVERALL_BUDGET_S,
    )

    orig_generate_2 = llm_client_2.generate_answer

    def timed_generate_2(**kwargs: Any) -> Dict[str, Any]:
        clock_2.advance(10.0)  # Elapsed 10.0 s -> remaining 10.0 s
        return orig_generate_2(**kwargs)

    llm_client_2.generate_answer = timed_generate_2  # type: ignore[method-assign]

    res_2 = faq_agent_2.resolve(
        ticket_id="TICK-BUDGET-RUN",
        session_id="sess-budget-run",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )

    assert len(llm_client_2.call_records) == 2  # Regeneration ran!
    assert res_2.status == "AUTO_RESOLVED"
    assert res_2.regeneration_count == 1
    # Check that regeneration call had capped deadline: min(15.0, 10.0 - 1.0) == 9.0 s
    regen_record = llm_client_2.call_records[1]
    assert regen_record.get("deadline_s") == pytest.approx(9.0, abs=0.05)


# REQ-AGENT-028
def test_passage_selector_pure_unit_tests():
    """Unit tests for the pure deterministic passage selector function.

    # REQ-AGENT-028: Grounded Static-Passage Fallback When Answer Generation Stalls.
    """
    article = (
        "Global Retail Hub offers hassle-free retail support. "
        "Standard returns are accepted within 30 days of delivery for a full refund. "
        "Items must be unopened in original packaging with receipt. "
        "Holiday return periods are extended through January 31."
    )

    # 1. Matches highest lexical overlap
    passage_1 = select_static_passage("How many days do I have to return standard items?", article)
    assert "within 30 days of delivery" in passage_1
    assert len(passage_1) <= 600

    # 2. Fits within character limit
    huge_sentence = "A" * 800 + "."
    passage_huge = select_static_passage("query", huge_sentence, max_chars=600)
    assert len(passage_huge) <= 600

    # 3. Handles empty or whitespace
    assert select_static_passage("query", "") == ""
    assert select_static_passage("query", "   \n\t  ") == ""

    # 4. Fallback when 0 query overlap
    passage_zero = select_static_passage("completely irrelevant xyz 123", article)
    assert "Global Retail Hub offers hassle-free" in passage_zero

    # 5. Stopword-light handling
    passage_stop = select_static_passage("what is the when of it", article)
    assert len(passage_stop) > 0


# REQ-AGENT-028
def test_validator_acceptance_of_fallback_citation():
    """Verify that build_static_fallback_payload always satisfies CitationValidator."""
    doc = {
        "id": "faq-shipping-01",
        "title": "Shipping Speeds & Rates",
        "content": (
            "Standard shipping takes 3 to 5 business days and is free on orders over $35. "
            "Express 2-day delivery is available for $9.99 per order. "
            "Overnight shipping delivers next business day for $19.99."
        ),
    }
    payload = build_static_fallback_payload(
        query="How much does express shipping cost?",
        top_doc=doc,
    )
    assert "Shipping Speeds & Rates" in payload["answer"]
    assert "Express 2-day delivery is available for $9.99" in payload["answer"]

    validator = CitationValidator()
    docs_map = {"faq-shipping-01": doc}
    res = validator.validate(payload, docs_map)
    assert res.is_valid is True
    assert len(res.citations) == 1
    assert res.citations[0].doc_id == "faq-shipping-01"


# REQ-AGENT-028
def test_ineligible_cases_still_escalate():
    """Verify that ineligible queries still escalate to human queues.

    Ineligible cases per REQ-AGENT-028:
    1. No docs retrieved
    2. Top-ranked doc relevance score < static_fallback_min_score
    3. Order-specific inquiries (predicted_intent='order_status' or order_id in message or order_info)
    """
    timeout_client = TimeoutLLMClient()

    # Case 1: No documents retrieved
    class EmptySearchClient:
        def search(self, **kwargs: Any) -> SearchResult:
            return SearchResult(
                document_ids=[],
                latency_ms=10.0,
                total_size=0,
                filter_applied=None,
                fallback_triggered=False,
                rerank_applied=False,
                raw_results=[],
                summary={},
            )

    agent_no_docs = FaqResolverAgent(
        search_client=EmptySearchClient(),
        llm_client=timeout_client,  # type: ignore[arg-type]
    )
    res_no_docs = agent_no_docs.resolve(
        ticket_id="TICK-NO-DOCS",
        session_id="sess-no-docs",
        message="What is the policy?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="shipping_policy",
    )
    assert res_no_docs.status == "ESCALATED"
    assert res_no_docs.route == "human"
    assert res_no_docs.reason == "uncited_answer"

    # Case 2: Low score document (score 0.20 < static_fallback_min_score 0.50)
    low_score_search = FakeSearchClient(
        docs={
            "faq-vague": {
                "title": "General Info",
                "content": "Some vague company info.",
                "score": 0.20,
            }
        }
    )
    agent_low_score = FaqResolverAgent(
        search_client=low_score_search,
        llm_client=timeout_client,  # type: ignore[arg-type]
    )
    res_low_score = agent_low_score.resolve(
        ticket_id="TICK-LOW-SCORE",
        session_id="sess-low-score",
        message="What is the policy?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="shipping_policy",
    )
    assert res_low_score.status == "ESCALATED"
    assert res_low_score.route == "human"
    assert res_low_score.reason == "uncited_answer"

    # Case 3: Order-specific intent
    agent_order_intent = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=timeout_client,  # type: ignore[arg-type]
    )
    res_order_intent = agent_order_intent.resolve(
        ticket_id="TICK-ORDER-INTENT",
        session_id="sess-order-intent",
        message="Where is my parcel?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="order_status",
    )
    assert res_order_intent.status == "ESCALATED"
    assert res_order_intent.route == "human"
    assert res_order_intent.reason == "uncited_answer"

    # Case 4: Order ID in message
    res_order_id = agent_order_intent.resolve(
        ticket_id="TICK-ORDER-ID",
        session_id="sess-order-id",
        message="Can you check status for ORD-998877?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="shipping_policy",
    )
    assert res_order_id.status == "ESCALATED"
    assert res_order_id.route == "human"


# REQ-AGENT-028
def test_model_armor_blocked_on_fallback_still_escalates():
    """Verify that Model Armor blocks or degrades static fallback if egress check fails."""
    timeout_client = TimeoutLLMClient()
    search_client = FakeSearchClient()

    # Sub-case 1: Blocked
    fake_ma_blocked = MagicMock()
    fake_ma_blocked.sanitize_model_response.return_value = ModelArmorVerdict(
        is_clean=False,
        blocked=True,
        action="BLOCK",
    )
    agent_blocked = FaqResolverAgent(
        search_client=search_client,
        llm_client=timeout_client,  # type: ignore[arg-type]
        model_armor_client=fake_ma_blocked,
    )
    res_blocked = agent_blocked.resolve(
        ticket_id="TICK-MA-BLOCKED",
        session_id="sess-ma-blocked",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )
    assert res_blocked.status == "ESCALATED"
    assert res_blocked.route == "human"
    assert res_blocked.reason == "model_armor_response_blocked"
    assert res_blocked.degraded is False

    # Sub-case 2: Degraded
    fake_ma_degraded = MagicMock()
    fake_ma_degraded.sanitize_model_response.return_value = ModelArmorVerdict(
        is_clean=False,
        blocked=False,
        action="ALLOW",
        degraded=True,
    )
    agent_degraded = FaqResolverAgent(
        search_client=search_client,
        llm_client=timeout_client,  # type: ignore[arg-type]
        model_armor_client=fake_ma_degraded,
    )
    res_degraded = agent_degraded.resolve(
        ticket_id="TICK-MA-DEGRADED",
        session_id="sess-ma-degraded",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )
    assert res_degraded.status == "ESCALATED"
    assert res_degraded.route == "human"
    assert res_degraded.reason == "model_armor_degraded"
    assert res_degraded.degraded is True


# REQ-AGENT-029
def test_hedge_constant_and_deadline_cap_values():
    """Verify ANSWER_HEDGE_AFTER_S constant equals 4.0 and deadline cap formula.

    # REQ-AGENT-029: Earlier Answer Hedge and Budget-Aware Citation Regeneration.
    """
    assert ANSWER_HEDGE_AFTER_S == 4.0
    assert ANSWER_DEADLINE_S == 15.0

    # Cap formula: min(ANSWER_DEADLINE_S, remaining_budget - 1.0)
    assert min(ANSWER_DEADLINE_S, 20.0 - 1.0) == 15.0
    assert min(ANSWER_DEADLINE_S, 10.0 - 1.0) == 9.0
    assert min(ANSWER_DEADLINE_S, 7.0 - 1.0) == 6.0


# REQ-AGENT-028, REQ-AGENT-029
def test_static_fallback_on_regeneration_timeout():
    """Verify static fallback is used when regeneration times out after uncited first call."""
    llm_client = ControllableLLMClient(fail_first=True, fail_regen_with_timeout=True)
    faq_agent = FaqResolverAgent(
        search_client=FakeSearchClient(),
        llm_client=llm_client,
    )

    res = faq_agent.resolve(
        ticket_id="TICK-REGEN-TIMEOUT",
        session_id="sess-regen-timeout",
        message="What is the return policy window?",
        customer_id="cust-001",
        customer_verified=True,
        predicted_intent="returns_refunds",
    )

    assert res.status == "AUTO_RESOLVED"
    assert res.route == "resolved"
    assert res.degraded is True
    assert res.reason == "static_fallback"
    assert len(llm_client.call_records) == 2
    assert res.regeneration_count == 1
