"""agent.py — Bounded ReAct FaqResolverAgent with mandatory citations.

REQ-AGENT-007: FaqResolver Bounded ReAct Loop, Search & Laya Rerank Integration.
- <= 6 tool calls
- <= 2 retrieval passes
- <= 1 answer regeneration
- 20-second wall-clock budget (injectable clock)
- Production search config 'intent_soft_noisy_0.2'
- Cross-encoder rerank via router-s1
- order-lookup-mcp integration with BOLA/IDOR protection

REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback.
- >= 1 verbatim citation quote matching retrieved article text
- 1 failed regeneration -> handoff {route: "human", reason: "uncited_answer"}
- Agent Search failure -> handoff {route: "human", degraded: True}
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from ticket_router.agents.faq_resolver.mcp_client import (
    BolaViolationError,
    FakeOrderLookupClient,
    OrderLookupProtocol,
)
from ticket_router.agents.faq_resolver.protocols import (
    Clock,
    LLMClientProtocol,
    RerankerProtocol,
    SearchClientProtocol,
)
from ticket_router.agents.faq_resolver.search import (
    execute_faq_search,
)
from ticket_router.agents.faq_resolver.validator import CitationValidator
from ticket_router.common.resilience import (
    CircuitBreaker,
    FAQ_OVERALL_BUDGET_S,
)
from ticket_router.config.models import model_id


@dataclass
class ResolutionResult:
    """Outcome of FaqResolverAgent inquiry resolution."""

    status: str  # "AUTO_RESOLVED" | "ESCALATED"
    route: str  # "resolved" | "human"
    queue: Optional[str] = None  # "human-standard" when escalated
    answer: Optional[str] = None
    citations: List[Dict[str, str]] = field(default_factory=list)
    reason: Optional[str] = None  # "uncited_answer", "search_failure", "budget_exceeded"
    degraded: bool = False
    message: Optional[str] = None
    tool_calls_count: int = 0
    retrieval_passes_count: int = 0
    regeneration_count: int = 0
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "route": self.route,
            "queue": self.queue,
            "answer": self.answer,
            "citations": self.citations,
            "reason": self.reason,
            "degraded": self.degraded,
            "message": self.message,
            "tool_calls_count": self.tool_calls_count,
            "retrieval_passes_count": self.retrieval_passes_count,
            "regeneration_count": self.regeneration_count,
            "latency_ms": self.latency_ms,
        }


class FakeLLMClient(LLMClientProtocol):
    """In-memory fake LLM client synthesizing answers with citations."""

    def __init__(
        self,
        default_answer: str = "Standard returns are accepted within 30 days of delivery.",
        default_quote: Optional[str] = None,
        default_doc_id: Optional[str] = None,
        fail_citations_once: bool = False,
        always_fail_citations: bool = False,
    ) -> None:
        self.default_answer = default_answer
        self.default_quote = default_quote
        self.default_doc_id = default_doc_id
        self.fail_citations_once = fail_citations_once
        self.always_fail_citations = always_fail_citations
        self.call_count = 0
        self.history: List[Dict[str, Any]] = []

    def generate_answer(
        self,
        query: str,
        retrieved_docs: Sequence[Dict[str, Any]],
        order_info: Optional[Dict[str, Any]] = None,
        model_id: Optional[str] = None,
        system_instruction: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.call_count += 1
        self.history.append({
            "query": query,
            "docs": list(retrieved_docs),
            "order_info": order_info,
            "feedback": feedback,
            "model_id": model_id,
        })

        if self.always_fail_citations or (self.fail_citations_once and self.call_count == 1):
            # Return ungrounded/paraphrased citation
            return {
                "answer": "You can return items anytime you feel like it.",
                "citations": [{"doc_id": "non_existent_doc", "quote": "fake quote"}],
            }

        # Pick doc_id and verbatim quote from retrieved_docs
        doc_id = self.default_doc_id
        quote = self.default_quote

        if not doc_id and retrieved_docs:
            first_doc = retrieved_docs[0]
            doc_id = first_doc.get("id") or first_doc.get("doc_id", "faq-general")
            content = first_doc.get("content") or first_doc.get("title", "")
            # Pick first 30 chars as verbatim quote
            quote = content[:40].strip() if content else "Standard policy"

        citations = []
        if doc_id and quote:
            citations.append({"doc_id": doc_id, "quote": quote})

        return {
            "answer": self.default_answer,
            "citations": citations,
        }


class FaqResolverAgent:
    """FaqResolverAgent executes a bounded ReAct retrieval and answering loop.

    Constraints (REQ-AGENT-007, REQ-AGENT-008, REQ-AGENT-012):
    - Tool calls <= 6
    - Retrieval passes <= 2
    - Answer regenerations <= 1
    - Overall wall-clock budget <= 20.0 seconds
    """

    def __init__(
        self,
        search_client: Optional[SearchClientProtocol] = None,
        reranker: Optional[RerankerProtocol] = None,
        order_client: Optional[OrderLookupProtocol] = None,
        llm_client: Optional[LLMClientProtocol] = None,
        validator: Optional[CitationValidator] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        clock: Optional[Clock] = None,
        model_role: str = "answer",
        total_budget_s: float = FAQ_OVERALL_BUDGET_S,
        max_tool_calls: int = 6,
        max_retrieval_passes: int = 2,
        max_regenerations: int = 1,
    ) -> None:
        self.search_client = search_client
        self.reranker = reranker
        self.order_client = order_client or FakeOrderLookupClient()
        self.llm_client = llm_client or FakeLLMClient()
        self.validator = validator or CitationValidator()
        self.circuit_breaker = circuit_breaker
        self._clock: Clock = clock if clock is not None else time.monotonic
        self.model_role = model_role
        self.total_budget_s = total_budget_s
        self.max_tool_calls = max_tool_calls
        self.max_retrieval_passes = max_retrieval_passes
        self.max_regenerations = max_regenerations

        # Resolve model ID dynamically from configuration
        self.resolved_model_id = model_id(self.model_role)

    def _extract_order_id(self, message: str) -> Optional[str]:
        """Extract redacted or plain order IDs from customer message."""
        # Match [ORDER_1], [ORDER_123], #12345, order 12345
        m = re.search(r"\[ORDER_(\w+)\]", message, re.IGNORECASE)
        if m:
            return f"ORDER_{m.group(1)}"
        m2 = re.search(r"\border\s*#?([A-Za-z0-9_-]{4,20})\b", message, re.IGNORECASE)
        if m2:
            return m2.group(1)
        return None

    def resolve(
        self,
        ticket_id: str,
        session_id: str,
        message: str,
        customer_id: str,
        predicted_intent: Optional[str] = None,
        memory_summary: Optional[str] = None,
        traceparent: Optional[str] = None,
        untrusted_customer_id: Optional[str] = None,
    ) -> ResolutionResult:
        """Execute bounded ReAct loop to resolve FAQ or order inquiry."""
        t_start = self._clock()

        def elapsed_ms() -> float:
            return round((self._clock() - t_start) * 1000.0, 2)

        def is_budget_exhausted() -> bool:
            return (self._clock() - t_start) >= self.total_budget_s

        tool_calls_count = 0
        retrieval_passes_count = 0
        regeneration_count = 0

        # Enforce initial budget check
        if is_budget_exhausted():
            return ResolutionResult(
                status="ESCALATED",
                route="human",
                queue="human-standard",
                reason="budget_exceeded",
                degraded=False,
                message="Turn budget exceeded before processing.",
                tool_calls_count=0,
                retrieval_passes_count=0,
                regeneration_count=0,
                latency_ms=elapsed_ms(),
            )

        # -----------------------------------------------------------------
        # Step 1: Order Lookup (if inquiry requires order status)
        # -----------------------------------------------------------------
        order_info: Optional[Dict[str, Any]] = None
        order_id = self._extract_order_id(message)

        if order_id and self.order_client is not None:
            if tool_calls_count < self.max_tool_calls and not is_budget_exhausted():
                tool_calls_count += 1
                try:
                    # BOLA / IDOR Protection: customer_id is verified session context
                    order_info = self.order_client.get_order(
                        order_id=order_id,
                        customer_id=customer_id,
                        untrusted_customer_id=untrusted_customer_id,
                    )
                except BolaViolationError:
                    # Security violation: immediate escalation without exposing details
                    return ResolutionResult(
                        status="ESCALATED",
                        route="human",
                        queue="human-standard",
                        reason="bola_violation",
                        degraded=False,
                        message="Inquiry routed to human specialist for account verification.",
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=0,
                        regeneration_count=0,
                        latency_ms=elapsed_ms(),
                    )
                except LookupError:
                    order_info = {"order_id": order_id, "status": "NOT_FOUND"}

        # -----------------------------------------------------------------
        # Step 2: Agent Search Retrieval Pass
        # -----------------------------------------------------------------
        retrieved_docs_map: Dict[str, Dict[str, Any]] = {}
        ordered_doc_ids: List[str] = []

        if self.search_client is not None:
            if retrieval_passes_count < self.max_retrieval_passes and tool_calls_count < self.max_tool_calls:
                retrieval_passes_count += 1
                tool_calls_count += 1

                search_res = execute_faq_search(
                    client=self.search_client,
                    query=message,
                    intent_family=predicted_intent,
                    circuit_breaker=self.circuit_breaker,
                )

                # Agent Search failure -> degraded escalation
                if not search_res.success or search_res.degraded:
                    return ResolutionResult(
                        status="ESCALATED",
                        route="human",
                        queue="human-standard",
                        reason="search_failure",
                        degraded=True,
                        message="Our automated knowledge base is temporarily degraded. A support specialist will assist you.",
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=retrieval_passes_count,
                        regeneration_count=regeneration_count,
                        latency_ms=elapsed_ms(),
                    )

                for doc_id, doc in search_res.documents.items():
                    retrieved_docs_map[doc_id] = {
                        "id": doc.id,
                        "title": doc.title,
                        "content": doc.content,
                    }
                ordered_doc_ids = list(search_res.document_ids)

        # -----------------------------------------------------------------
        # Step 3: Candidate Reranking via router-s1
        # -----------------------------------------------------------------
        if self.reranker is not None and retrieved_docs_map:
            if tool_calls_count < self.max_tool_calls and not is_budget_exhausted():
                tool_calls_count += 1
                candidates = [retrieved_docs_map[did] for did in ordered_doc_ids if did in retrieved_docs_map]
                reranked, _ = self.reranker.rerank(query=message, candidates=candidates)
                if reranked:
                    ordered_doc_ids = [r["id"] for r in reranked if "id" in r]

        # -----------------------------------------------------------------
        # Step 4: Grounded Answer Synthesis & Mandatory Citations
        # -----------------------------------------------------------------
        if is_budget_exhausted():
            return ResolutionResult(
                status="ESCALATED",
                route="human",
                queue="human-standard",
                reason="budget_exceeded",
                degraded=False,
                message="Turn budget exceeded during knowledge retrieval.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=elapsed_ms(),
            )

        candidate_docs_list = [retrieved_docs_map[did] for did in ordered_doc_ids if did in retrieved_docs_map]

        # Generate initial answer
        answer_payload = self.llm_client.generate_answer(
            query=message,
            retrieved_docs=candidate_docs_list,
            order_info=order_info,
            model_id=self.resolved_model_id,
        )

        val_result = self.validator.validate(answer_payload, retrieved_docs_map)

        # -----------------------------------------------------------------
        # Step 5: Conditional 1-Time Answer Regeneration
        # -----------------------------------------------------------------
        if not val_result.is_valid and regeneration_count < self.max_regenerations and not is_budget_exhausted():
            regeneration_count += 1
            feedback = (
                f"Citation validation rejected previous response: {val_result.reason} ({val_result.details}). "
                "You MUST supply at least one citation with a quote verbatim present in the retrieved articles."
            )
            answer_payload = self.llm_client.generate_answer(
                query=message,
                retrieved_docs=candidate_docs_list,
                order_info=order_info,
                model_id=self.resolved_model_id,
                feedback=feedback,
            )
            val_result = self.validator.validate(answer_payload, retrieved_docs_map)

        # -----------------------------------------------------------------
        # Step 6: Non-Hallucination Fallback or Auto-Resolution
        # -----------------------------------------------------------------
        if not val_result.is_valid:
            # Enforce non-hallucination fallback: NEVER return ungrounded answer
            return ResolutionResult(
                status="ESCALATED",
                route="human",
                queue="human-standard",
                reason="uncited_answer",
                degraded=False,
                message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=elapsed_ms(),
            )

        return ResolutionResult(
            status="AUTO_RESOLVED",
            route="resolved",
            answer=answer_payload.get("answer", ""),
            citations=[c.to_dict() for c in val_result.citations],
            degraded=False,
            tool_calls_count=tool_calls_count,
            retrieval_passes_count=retrieval_passes_count,
            regeneration_count=regeneration_count,
            latency_ms=elapsed_ms(),
        )


# ADK wiring: export default root_agent instance
root_agent = FaqResolverAgent()
