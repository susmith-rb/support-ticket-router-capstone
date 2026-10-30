"""agent.py — Bounded ReAct FaqResolverAgent with mandatory citations.

# REQ-AGENT-007: FaqResolver Bounded ReAct Loop, Search & Laya Rerank Integration.
# - <= 6 tool calls
# - <= 2 retrieval passes
# - <= 1 answer regeneration
# - 20-second wall-clock budget (injectable clock)
# - Production search config 'intent_soft_noisy_0.2'
# - Cross-encoder rerank via router-s1
# - order-lookup-mcp integration with BOLA/IDOR protection
#
# REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback.
# - >= 1 verbatim citation quote matching retrieved article text
# - 1 failed regeneration -> handoff {route: "human", reason: "uncited_answer"}
# - Agent Search failure -> handoff {route: "human", degraded: True}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ticket_router.agents.faq_resolver.mcp_client import (
    BolaViolationError,
    FakeOrderLookupClient,
    HttpOrderLookupClient,
    OrderLookupProtocol,
)
from ticket_router.agents.faq_resolver.passage_selector import (
    build_static_fallback_payload,
)
from ticket_router.agents.faq_resolver.protocols import (
    Clock,
    LLMClientProtocol,
    RerankerProtocol,
    SearchClientProtocol,
)
from ticket_router.agents.faq_resolver.reranker import (
    VertexLayaReranker,
)
from ticket_router.agents.faq_resolver.search import (
    execute_faq_search,
)
from ticket_router.agents.faq_resolver.validator import CitationValidator
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
    InMemoryTicketDbMcpClient,
)
from ticket_router.agents.triage_router.templates import render_calming_acknowledgement
from ticket_router.common.hedging import hedged_call
from ticket_router.common.observability import start_span
from ticket_router.common.resilience import (
    ANSWER_DEADLINE_S,
    ANSWER_HEDGE_AFTER_S,
    FAQ_OVERALL_BUDGET_S,
    CircuitBreaker,
)
from ticket_router.common.settings import Settings
from ticket_router.config.models import get, model_id, model_thinking_level
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher, PubSubRestPublisher
from ticket_router.models.router_s1_client import RouterS1Client
from ticket_router.security.model_armor import (
    ModelArmorClient,
    ModelArmorClientProtocol,
)

logger = logging.getLogger(__name__)


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
    handoff_done: bool = False
    calming_ack: Optional[str] = None

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
            "handoff_done": self.handoff_done,
            "calming_ack": self.calming_ack,
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
        deadline_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.call_count += 1
        self.history.append({
            "query": query,
            "docs": list(retrieved_docs),
            "order_info": order_info,
            "feedback": feedback,
            "model_id": model_id,
            "deadline_s": deadline_s,
        })

        if self.always_fail_citations or (self.fail_citations_once and self.call_count == 1):
            return {
                "answer": "You can return items anytime you feel like it.",
                "citations": [{"doc_id": "non_existent_doc", "quote": "fake quote"}],
            }

        doc_id = self.default_doc_id
        quote = self.default_quote

        if not doc_id and retrieved_docs:
            first_doc = retrieved_docs[0]
            doc_id = first_doc.get("id") or first_doc.get("doc_id", "faq-general")
            content = first_doc.get("content") or first_doc.get("title", "")
            quote = content[:40].strip() if content else "Standard policy"

        citations = []
        if doc_id and quote:
            citations.append({"doc_id": doc_id, "quote": quote})

        return {
            "answer": self.default_answer,
            "citations": citations,
        }


class GeminiAnswerClient(LLMClientProtocol):
    """Production LLM client synthesizing answers with mandatory citations via google-genai."""

    def __init__(
        self,
        project: str,
        location: str,
        model_role: str = "answer",
    ) -> None:
        self.project = project
        self.model_role = model_role
        try:
            model_cfg = get(self.model_role)
            self.location = model_cfg.get("location") or location
            self.resolved_model_id = str(model_cfg.get("id") or model_id(self.model_role))
        except Exception:
            self.location = location
            self.resolved_model_id = model_id(self.model_role)
        self._client: Optional[Any] = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # type: ignore[import-untyped]
            self._client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
            )
        return self._client

    def generate_answer(
        self,
        query: str,
        retrieved_docs: Sequence[Dict[str, Any]],
        order_info: Optional[Dict[str, Any]] = None,
        model_id: Optional[str] = None,
        system_instruction: Optional[str] = None,
        feedback: Optional[str] = None,
        deadline_s: Optional[float] = None,
        hedge_after_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Synthesize answer with mandatory citations based strictly on retrieved docs."""
        client = self._get_client()
        docs_context = json.dumps(list(retrieved_docs), indent=2)
        order_context = json.dumps(order_info or {}, indent=2)

        from ticket_router.config.agent_tuning import get_faq_answer_prompt
        prompt = get_faq_answer_prompt(
            query=query,
            docs_context=docs_context,
            order_context=order_context,
            feedback=feedback,
        )

        target_model = model_id or self.resolved_model_id
        thinking_lvl = model_thinking_level(self.model_role)
        call_kwargs: Dict[str, Any] = {
            "model": target_model,
            "contents": prompt,
        }
        if thinking_lvl:
            try:
                from google.genai import types as genai_types
                if hasattr(genai_types, "GenerateContentConfig") and hasattr(genai_types, "ThinkingConfig"):
                    call_kwargs["config"] = genai_types.GenerateContentConfig(
                        thinking_config=genai_types.ThinkingConfig(thinking_level=thinking_lvl)
                    )
            except Exception:
                pass

        def _invoke_generate() -> Any:
            call_args = dict(call_kwargs)
            try:
                return client.models.generate_content(**call_args)
            except TypeError:
                call_args.pop("config", None)
                return client.models.generate_content(**call_args)

        effective_deadline = deadline_s if deadline_s is not None else ANSWER_DEADLINE_S
        effective_hedge = hedge_after_s if hedge_after_s is not None else ANSWER_HEDGE_AFTER_S

        resp = hedged_call(
            _invoke_generate,
            effective_hedge,
            effective_deadline,
            span_name="faq_resolver.generate_content",
        )
        raw_text = ""
        try:
            raw_text = str(resp.text)
        except AttributeError:
            raw_text = ""

        clean_text = raw_text.strip()
        if clean_text.startswith("```"):
            lines = clean_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_text = "\n".join(lines).strip()

        try:
            parsed = json.loads(clean_text)
            if isinstance(parsed, dict) and "answer" in parsed:
                return parsed
        except Exception:
            pass

        return {
            "answer": clean_text,
            "citations": [],
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
        model_armor_client: Optional[ModelArmorClientProtocol] = None,
        clock: Optional[Clock] = None,
        model_role: str = "answer",
        total_budget_s: float = FAQ_OVERALL_BUDGET_S,
        max_tool_calls: int = 6,
        max_retrieval_passes: int = 2,
        max_regenerations: int = 1,
        lazy_init: bool = False,
        human_handoff: Optional[HumanHandoff] = None,
        ticket_db_client: Optional[Any] = None,
        publisher: Optional[Any] = None,
    ) -> None:
        self.search_client = search_client
        self.reranker = reranker
        self.order_client = order_client
        self.llm_client = llm_client
        self.validator = validator or CitationValidator()
        self.circuit_breaker = circuit_breaker
        self.model_armor_client = model_armor_client
        self._clock: Clock = clock if clock is not None else time.monotonic
        self.model_role = model_role
        self.total_budget_s = total_budget_s
        self.max_tool_calls = max_tool_calls
        self.max_retrieval_passes = max_retrieval_passes
        self.max_regenerations = max_regenerations
        self._lazy_init = lazy_init
        self.human_handoff = human_handoff
        self.ticket_db_client = ticket_db_client
        self.publisher = publisher

        # Resolve model ID dynamically from configuration
        self.resolved_model_id = model_id(self.model_role)
        if not lazy_init:
            self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Lazily initialize real dependencies from environment settings."""
        if not self._lazy_init:
            if self.order_client is None:
                self.order_client = FakeOrderLookupClient()
            if self.llm_client is None:
                self.llm_client = FakeLLMClient()
            if self.ticket_db_client is None:
                self.ticket_db_client = InMemoryTicketDbMcpClient()
            if self.publisher is None:
                self.publisher = FakePublisher()
            if self.human_handoff is None:
                self.human_handoff = HumanHandoff(
                    ticket_db_client=self.ticket_db_client,
                    publisher=self.publisher,
                    escalated_by="faq_resolver",
                )
            return

        settings = Settings.from_env()
        if self.ticket_db_client is None:
            if settings.require_real_deps:
                ticket_db_url = os.environ.get("TICKET_DB_MCP_URL") or settings.ticket_db_mcp_url or ""
                self.ticket_db_client = HttpTicketDbMcpClient(base_url=ticket_db_url)
            else:
                self.ticket_db_client = InMemoryTicketDbMcpClient()

        if self.publisher is None:
            if settings.require_real_deps:
                topic = os.environ.get("HUMAN_QUEUE_TOPIC", "human-queue")
                self.publisher = PubSubRestPublisher(
                    project=settings.project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    topic=topic,
                )
            else:
                self.publisher = FakePublisher()

        if self.human_handoff is None:
            self.human_handoff = HumanHandoff(
                ticket_db_client=self.ticket_db_client,
                publisher=self.publisher,
                escalated_by="faq_resolver",
            )
        if self.model_armor_client is None and settings.model_armor_enabled:
            self.model_armor_client = ModelArmorClient(
                template=settings.model_armor_template,
            )

        if settings.require_real_deps:
            settings.validate_for_faq()
            from ticket_router.retrieval.search_client import SearchClient
            self.search_client = SearchClient(
                project=settings.project,
                location=settings.agent_search_location,
                engine=settings.agent_search_engine_id,
            )
            s1_client = RouterS1Client(
                endpoint=settings.router_s1_endpoint,
                base_url=settings.router_s1_base_url,
            )
            self.reranker = VertexLayaReranker(client=s1_client)
            self.order_client = HttpOrderLookupClient(
                base_url=settings.order_lookup_mcp_url or "http://localhost:8080",
            )
            self.llm_client = GeminiAnswerClient(
                project=settings.project,
                location=settings.location,
                model_role=self.model_role,
            )
            if self.circuit_breaker is None:
                self.circuit_breaker = CircuitBreaker(name="agent_search")
        else:
            if self.order_client is None:
                self.order_client = FakeOrderLookupClient()
            if self.llm_client is None:
                self.llm_client = FakeLLMClient()
        self._lazy_init = False

    def _extract_order_id(self, message: str) -> Optional[str]:
        """Extract redacted or plain order IDs from customer message."""
        m = re.search(r"\[ORDER_(\w+)\]", message, re.IGNORECASE)
        if m:
            return f"ORDER_{m.group(1)}"
        m2 = re.search(r"\border\s*#?([A-Za-z0-9_-]{4,20})\b", message, re.IGNORECASE)
        if m2:
            return m2.group(1)
        return None

    def _elapsed_ms(self, t_start: float) -> float:
        """Return elapsed milliseconds since t_start."""
        return round((self._clock() - t_start) * 1000.0, 2)

    def _elapsed_s(self, t_start: float) -> float:
        """Return elapsed seconds since t_start."""
        return self._clock() - t_start

    def _is_budget_exhausted(self, t_start: float) -> bool:
        """Check if elapsed time since t_start exceeds total budget."""
        return self._elapsed_s(t_start) >= self.total_budget_s

    def _invoke_generate_answer(
        self,
        query: str,
        retrieved_docs: Sequence[Dict[str, Any]],
        order_info: Optional[Dict[str, Any]] = None,
        model_id: Optional[str] = None,
        system_instruction: Optional[str] = None,
        feedback: Optional[str] = None,
        deadline_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Invoke generate_answer on llm_client, safely propagating deadline_s."""
        assert self.llm_client is not None
        call_kwargs: Dict[str, Any] = {
            "query": query,
            "retrieved_docs": retrieved_docs,
            "order_info": order_info,
            "model_id": model_id,
        }
        if system_instruction is not None:
            call_kwargs["system_instruction"] = system_instruction
        if feedback is not None:
            call_kwargs["feedback"] = feedback
        if deadline_s is not None:
            call_kwargs["deadline_s"] = deadline_s

        try:
            return self.llm_client.generate_answer(**call_kwargs)
        except TypeError as exc:
            if "deadline_s" in str(exc) or "unexpected keyword" in str(exc):
                call_kwargs.pop("deadline_s", None)
                return self.llm_client.generate_answer(**call_kwargs)
            raise

    def _escalate(
        self,
        *,
        ticket_id: str,
        queue: str = "human-standard",
        reason: str,
        degraded: bool = False,
        message: Optional[str] = None,
        tool_calls_count: int = 0,
        retrieval_passes_count: int = 0,
        regeneration_count: int = 0,
        latency_ms: float = 0.0,
        traceparent: Optional[str] = None,
        customer_id: Optional[str] = None,
        session_id: Optional[str] = None,
        body_redacted: Optional[str] = None,
    ) -> ResolutionResult:
        """Execute shared HumanHandoff and return ESCALATED ResolutionResult (REQ-AGENT-030)."""
        calming_ack: Optional[str] = None
        handoff_done = False
        if self.human_handoff is not None:
            try:
                res = self.human_handoff.escalate(
                    ticket_id=ticket_id,
                    queue=queue,
                    reason=reason,
                    escalated_by="faq_resolver",
                    degraded=degraded,
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=body_redacted,
                )
                calming_ack = res.calming_ack
                handoff_done = True
            except Exception as exc:
                logger.warning("FaqResolver HumanHandoff failed: %s", exc)
                calming_ack = render_calming_acknowledgement(queue=queue, ticket_id=ticket_id)
                handoff_done = True
        else:
            calming_ack = render_calming_acknowledgement(queue=queue, ticket_id=ticket_id)

        return ResolutionResult(
            status="ESCALATED",
            route="human",
            queue=queue,
            reason=reason,
            degraded=degraded,
            message=message,
            tool_calls_count=tool_calls_count,
            retrieval_passes_count=retrieval_passes_count,
            regeneration_count=regeneration_count,
            latency_ms=latency_ms,
            handoff_done=handoff_done,
            calming_ack=calming_ack,
        )

    def _is_static_fallback_eligible(
        self,
        predicted_intent: Optional[str],
        order_info: Optional[Dict[str, Any]],
        order_id: Optional[str],
        ordered_doc_ids: Sequence[str],
        retrieved_docs_map: Dict[str, Dict[str, Any]],
    ) -> bool:
        """Evaluate eligibility for REQ-AGENT-028 static grounded fallback.

        Ineligible if:
        1. Order intents (order_status) or order_id or order_info present.
        2. No articles retrieved or retrieved doc has no content/title.
        3. Top-ranked article score < static_fallback_min_score from config.
        """
        if predicted_intent == "order_status" or bool(order_id) or bool(order_info):
            return False

        if not ordered_doc_ids or not retrieved_docs_map:
            return False

        top_doc_id = ordered_doc_ids[0]
        if top_doc_id not in retrieved_docs_map:
            return False

        top_doc = retrieved_docs_map[top_doc_id]
        content = str(
            top_doc.get("content") or top_doc.get("body") or top_doc.get("text") or ""
        ).strip()
        title = str(top_doc.get("title") or "").strip()
        if not content and not title:
            return False

        from ticket_router.config.agent_tuning import get_tuning_config
        cfg = get_tuning_config()
        min_score = getattr(cfg, "static_fallback_min_score", 0.50)

        # Inspect score fields on top_doc
        doc_score = top_doc.get("score")
        if doc_score is None:
            doc_score = top_doc.get("relevance_score")
        if doc_score is None:
            doc_score = top_doc.get("relevanceScore")
        if doc_score is None:
            raw = top_doc.get("raw", {})
            if isinstance(raw, dict):
                doc_score = raw.get("score") or raw.get("relevance_score") or raw.get("relevanceScore")

        if doc_score is not None:
            try:
                if float(doc_score) < float(min_score):
                    return False
            except (ValueError, TypeError):
                pass

        return True

    def _execute_static_fallback(
        self,
        message: str,
        ordered_doc_ids: Sequence[str],
        retrieved_docs_map: Dict[str, Dict[str, Any]],
        tool_calls_count: int,
        retrieval_passes_count: int,
        regeneration_count: int,
        t_start: float,
        ticket_id: str = "",
        traceparent: Optional[str] = None,
        customer_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ResolutionResult:
        """Produce deterministic static grounded fallback per REQ-AGENT-028."""
        top_doc_id = ordered_doc_ids[0]
        top_doc = retrieved_docs_map[top_doc_id]

        fallback_payload = build_static_fallback_payload(query=message, top_doc=top_doc)
        val_result = self.validator.validate(fallback_payload, retrieved_docs_map)

        if not val_result.is_valid:
            return self._escalate(
                ticket_id=ticket_id,
                queue="human-standard",
                reason="uncited_answer",
                degraded=True,
                message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=self._elapsed_ms(t_start),
                traceparent=traceparent,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=message,
            )

        raw_answer = fallback_payload.get("answer", "")
        if self.model_armor_client is not None:
            ma_verdict = self.model_armor_client.sanitize_model_response(raw_answer)
            if ma_verdict.blocked:
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="model_armor_response_blocked",
                    degraded=False,
                    message="To ensure your issue is safely and accurately handled, I have escalated your ticket to our customer support specialists.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )
            if ma_verdict.degraded:
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="model_armor_degraded",
                    degraded=True,
                    message="Our system is temporarily experiencing technical delays. A support specialist will follow up with you.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )

        return ResolutionResult(
            status="AUTO_RESOLVED",
            route="resolved",
            answer=raw_answer,
            citations=[c.to_dict() for c in val_result.citations],
            reason="static_fallback",
            degraded=True,
            tool_calls_count=tool_calls_count,
            retrieval_passes_count=retrieval_passes_count,
            regeneration_count=regeneration_count,
            latency_ms=self._elapsed_ms(t_start),
        )

    def resolve(
        self,
        ticket_id: str,
        session_id: str,
        message: str,
        customer_id: Optional[str] = None,
        customer_verified: Optional[bool] = None,
        identity_issuer: Optional[str] = None,
        predicted_intent: Optional[str] = None,
        memory_summary: Optional[str] = None,
        traceparent: Optional[str] = None,
        untrusted_customer_id: Optional[str] = None,
        customer_context_token: Optional[str] = None,
    ) -> ResolutionResult:
        """Execute bounded ReAct loop to resolve FAQ or order inquiry."""
        self._ensure_initialized()

        with start_span(
            "faq_resolver.resolve",
            attributes={
                "ticket_id": ticket_id,
                "session_id": session_id,
                "predicted_intent": predicted_intent or "",
            },
            traceparent=traceparent,
        ) as resolve_span:
            res = self._resolve_impl(
                ticket_id=ticket_id,
                session_id=session_id,
                message=message,
                customer_id=customer_id,
                customer_verified=customer_verified,
                identity_issuer=identity_issuer,
                predicted_intent=predicted_intent,
                memory_summary=memory_summary,
                untrusted_customer_id=untrusted_customer_id,
                customer_context_token=customer_context_token,
                traceparent=traceparent,
            )
            resolve_span.set_attribute("status", res.status)
            return res

    def _resolve_impl(
        self,
        ticket_id: str,
        session_id: str,
        message: str,
        customer_id: Optional[str] = None,
        customer_verified: Optional[bool] = None,
        identity_issuer: Optional[str] = None,
        predicted_intent: Optional[str] = None,
        memory_summary: Optional[str] = None,
        untrusted_customer_id: Optional[str] = None,
        customer_context_token: Optional[str] = None,
        traceparent: Optional[str] = None,
    ) -> ResolutionResult:
        t_start = self._clock()

        tool_calls_count = 0
        retrieval_passes_count = 0
        regeneration_count = 0

        # Enforce initial budget check
        if self._is_budget_exhausted(t_start):
            return self._escalate(
                ticket_id=ticket_id,
                queue="human-standard",
                reason="budget_exceeded",
                degraded=False,
                message="Turn budget exceeded before processing.",
                tool_calls_count=0,
                retrieval_passes_count=0,
                regeneration_count=0,
                latency_ms=self._elapsed_ms(t_start),
                traceparent=traceparent,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=message,
            )

        # Determine verification status (REQ-IDENT-004, REQ-IDENT-005)
        is_verified = (
            customer_verified
            if customer_verified is not None
            else bool(customer_id and not str(customer_id).startswith("anon:"))
        )

        # -----------------------------------------------------------------
        # Step 1: Order Lookup (if inquiry requires order status)
        # -----------------------------------------------------------------
        order_info: Optional[Dict[str, Any]] = None
        order_id = self._extract_order_id(message)
        is_order_specific = bool(order_id) or (
            predicted_intent is not None
            and predicted_intent in (
                "shipping",
                "refund",
                "order_status",
                "shipping_policy",
                "returns_refunds",
            )
            and any(
                kw in message.lower()
                for kw in (
                    "order",
                    "orders",
                    "package",
                    "packages",
                    "tracking",
                    "status",
                    "delivery",
                    "item",
                    "items",
                    "where is",
                    "track",
                    "recent",
                    "purchase",
                    "purchases",
                    "bought",
                    "shipment",
                    "shipments",
                    "history",
                )
            )
        )

        if not is_verified:
            if is_order_specific:
                # REQ-IDENT-004, REQ-IDENT-005: Unverified customer attempting order-specific inquiry
                # Never invoke order lookup; return polite sign-in prompt with human handoff option
                return ResolutionResult(
                    status="AUTO_RESOLVED",
                    route="resolved",
                    answer=(
                        "Please sign in to check your order details and track shipments. "
                        "If you need further assistance, I can also connect you with a human support specialist."
                    ),
                    citations=[],
                    degraded=False,
                    tool_calls_count=0,
                    retrieval_passes_count=0,
                    regeneration_count=0,
                    latency_ms=self._elapsed_ms(t_start),
                )
            # If not order-specific, unverified customer continues to Step 2 for generic FAQ answers
        else:
            # Verified customer: invoke order lookup if order_id present
            if (
                order_id
                and self.order_client is not None
                and tool_calls_count < self.max_tool_calls
                and not self._is_budget_exhausted(t_start)
            ):
                tool_calls_count += 1
                try:
                    # BOLA / IDOR Protection: customer_id is verified session context
                    get_order_kwargs: Dict[str, Any] = {
                        "order_id": order_id,
                        "customer_id": customer_id or "",
                        "untrusted_customer_id": untrusted_customer_id,
                    }
                    if customer_context_token is not None and customer_context_token != "":
                        get_order_kwargs["customer_context_token"] = customer_context_token
                    order_info = self.order_client.get_order(**get_order_kwargs)
                except BolaViolationError:
                    return self._escalate(
                        ticket_id=ticket_id,
                        queue="human-standard",
                        reason="bola_violation",
                        degraded=False,
                        message="Inquiry routed to human specialist for account verification.",
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=0,
                        regeneration_count=0,
                        latency_ms=self._elapsed_ms(t_start),
                        traceparent=traceparent,
                        customer_id=customer_id,
                        session_id=session_id,
                        body_redacted=message,
                    )
                except LookupError:
                    order_info = {"order_id": order_id, "status": "NOT_FOUND"}
                except Exception as exc:
                    logger.warning("Order lookup failed gracefully: %s", exc)
                    order_info = {"order_id": order_id, "status": "UNAVAILABLE"}
            elif (
                is_order_specific
                and self.order_client is not None
                and hasattr(self.order_client, "list_orders")
                and tool_calls_count < self.max_tool_calls
                and not self._is_budget_exhausted(t_start)
            ):
                tool_calls_count += 1
                try:
                    with start_span("faq_resolver.list_orders", {"ticket_id": ticket_id}):
                        list_orders_kwargs: Dict[str, Any] = {
                            "customer_id": customer_id or "",
                            "limit": 5,
                        }
                        if customer_context_token is not None and customer_context_token != "":
                            list_orders_kwargs["customer_context_token"] = customer_context_token
                        listed = self.order_client.list_orders(**list_orders_kwargs)
                    if isinstance(listed, dict) and "orders" in listed:
                        if listed.get("orders"):
                            order_info = listed
                        else:
                            order_info = {"orders": [], "status": "NO_ORDERS_FOUND"}
                except BolaViolationError:
                    return self._escalate(
                        ticket_id=ticket_id,
                        queue="human-standard",
                        reason="bola_violation",
                        degraded=False,
                        message="Inquiry routed to human specialist for account verification.",
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=0,
                        regeneration_count=0,
                        latency_ms=self._elapsed_ms(t_start),
                        traceparent=traceparent,
                        customer_id=customer_id,
                        session_id=session_id,
                        body_redacted=message,
                    )
                except Exception as exc:
                    logger.warning("Natural order lookup (list_orders) failed gracefully: %s", exc)

        # -----------------------------------------------------------------
        # Step 2: Agent Search Retrieval Pass
        # -----------------------------------------------------------------
        retrieved_docs_map: Dict[str, Dict[str, Any]] = {}
        ordered_doc_ids: List[str] = []

        if (
            self.search_client is not None
            and retrieval_passes_count < self.max_retrieval_passes
            and tool_calls_count < self.max_tool_calls
        ):
            retrieval_passes_count += 1
            tool_calls_count += 1

            with start_span("faq_resolver.search"):
                search_res = execute_faq_search(
                    client=self.search_client,
                    query=message,
                    intent_family=predicted_intent,
                    circuit_breaker=self.circuit_breaker,
                )

            # Agent Search failure -> degraded escalation
            if not search_res.success or search_res.degraded:
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="search_failure",
                    degraded=True,
                    message="Our automated knowledge base is temporarily degraded. A support specialist will assist you.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )

            for doc_id, doc in search_res.documents.items():
                retrieved_docs_map[doc_id] = {
                    "id": doc.id,
                    "title": doc.title,
                    "content": doc.content,
                    "score": doc.score,
                    "raw": doc.raw,
                }
            ordered_doc_ids = list(search_res.document_ids)

        # -----------------------------------------------------------------
        # Step 3: Candidate Reranking via router-s1
        # -----------------------------------------------------------------
        if (
            self.reranker is not None
            and retrieved_docs_map
            and tool_calls_count < self.max_tool_calls
            and not self._is_budget_exhausted(t_start)
        ):
            tool_calls_count += 1
            candidates = [retrieved_docs_map[did] for did in ordered_doc_ids if did in retrieved_docs_map]
            with start_span("faq_resolver.rerank"):
                reranked, _ = self.reranker.rerank(query=message, candidates=candidates)
            if reranked:
                ordered_doc_ids = [r["id"] for r in reranked if "id" in r]
                for r in reranked:
                    rid = r.get("id")
                    if rid and rid in retrieved_docs_map and "score" in r:
                        retrieved_docs_map[rid]["score"] = r["score"]

        # -----------------------------------------------------------------
        # Step 4: Grounded Answer Synthesis & Mandatory Citations
        # -----------------------------------------------------------------
        if self._is_budget_exhausted(t_start):
            return self._escalate(
                ticket_id=ticket_id,
                queue="human-standard",
                reason="budget_exceeded",
                degraded=False,
                message="Turn budget exceeded during knowledge retrieval.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=self._elapsed_ms(t_start),
                traceparent=traceparent,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=message,
            )

        candidate_docs_list = [retrieved_docs_map[did] for did in ordered_doc_ids if did in retrieved_docs_map]

        if self.llm_client is None:
            self.llm_client = FakeLLMClient()

        llm_failed = False
        try:
            with start_span("faq_resolver.generate_answer"):
                answer_payload = self._invoke_generate_answer(
                    query=message,
                    retrieved_docs=candidate_docs_list,
                    order_info=order_info,
                    model_id=self.resolved_model_id,
                )
        except Exception as exc:
            logger.warning(
                "Answer synthesis failed (%s) for ticket %s",
                type(exc).__name__,
                ticket_id,
            )
            llm_failed = True
            answer_payload = {}

        if llm_failed:
            if self._is_static_fallback_eligible(
                predicted_intent=predicted_intent,
                order_info=order_info,
                order_id=order_id,
                ordered_doc_ids=ordered_doc_ids,
                retrieved_docs_map=retrieved_docs_map,
            ):
                return self._execute_static_fallback(
                    message=message,
                    ordered_doc_ids=ordered_doc_ids,
                    retrieved_docs_map=retrieved_docs_map,
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    t_start=t_start,
                    ticket_id=ticket_id,
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                )
            return self._escalate(
                ticket_id=ticket_id,
                queue="human-standard",
                reason="uncited_answer",
                degraded=True,
                message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=self._elapsed_ms(t_start),
                traceparent=traceparent,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=message,
            )

        val_result = self.validator.validate(answer_payload, retrieved_docs_map)

        # -----------------------------------------------------------------
        # Step 5: Conditional 1-Time Answer Regeneration (REQ-AGENT-029)
        # -----------------------------------------------------------------
        if (
            not llm_failed
            and not val_result.is_valid
            and regeneration_count < self.max_regenerations
        ):
            elapsed = self._elapsed_s(t_start)
            remaining_budget = self.total_budget_s - elapsed
            min_budget_for_regen = ANSWER_HEDGE_AFTER_S + 2.0

            if remaining_budget >= min_budget_for_regen:
                regeneration_count += 1
                regen_deadline = min(ANSWER_DEADLINE_S, remaining_budget - 1.0)
                feedback = (
                    f"Citation validation rejected previous response: {val_result.reason} ({val_result.details}). "
                    "You MUST supply at least one citation with a quote verbatim present in the retrieved articles."
                )
                try:
                    with start_span("faq_resolver.generate_answer"):
                        answer_payload = self._invoke_generate_answer(
                            query=message,
                            retrieved_docs=candidate_docs_list,
                            order_info=order_info,
                            model_id=self.resolved_model_id,
                            feedback=feedback,
                            deadline_s=regen_deadline,
                        )
                except Exception as exc:
                    logger.warning(
                        "Answer regeneration failed (%s) for ticket %s",
                        type(exc).__name__,
                        ticket_id,
                    )
                    llm_failed = True
                    answer_payload = {}

                if llm_failed:
                    if self._is_static_fallback_eligible(
                        predicted_intent=predicted_intent,
                        order_info=order_info,
                        order_id=order_id,
                        ordered_doc_ids=ordered_doc_ids,
                        retrieved_docs_map=retrieved_docs_map,
                    ):
                        return self._execute_static_fallback(
                            message=message,
                            ordered_doc_ids=ordered_doc_ids,
                            retrieved_docs_map=retrieved_docs_map,
                            tool_calls_count=tool_calls_count,
                            retrieval_passes_count=retrieval_passes_count,
                            regeneration_count=regeneration_count,
                            t_start=t_start,
                            ticket_id=ticket_id,
                            traceparent=traceparent,
                            customer_id=customer_id,
                            session_id=session_id,
                        )
                    return self._escalate(
                        ticket_id=ticket_id,
                        queue="human-standard",
                        reason="uncited_answer",
                        degraded=True,
                        message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=retrieval_passes_count,
                        regeneration_count=regeneration_count,
                        latency_ms=self._elapsed_ms(t_start),
                        traceparent=traceparent,
                        customer_id=customer_id,
                        session_id=session_id,
                        body_redacted=message,
                    )

                val_result = self.validator.validate(answer_payload, retrieved_docs_map)
            else:
                # REQ-AGENT-029: Not enough remaining budget for regeneration -> skip regeneration!
                logger.info(
                    "Skipping regeneration: remaining budget %.2f s < min required %.2f s",
                    remaining_budget,
                    min_budget_for_regen,
                )
                if self._is_static_fallback_eligible(
                    predicted_intent=predicted_intent,
                    order_info=order_info,
                    order_id=order_id,
                    ordered_doc_ids=ordered_doc_ids,
                    retrieved_docs_map=retrieved_docs_map,
                ):
                    return self._execute_static_fallback(
                        message=message,
                        ordered_doc_ids=ordered_doc_ids,
                        retrieved_docs_map=retrieved_docs_map,
                        tool_calls_count=tool_calls_count,
                        retrieval_passes_count=retrieval_passes_count,
                        regeneration_count=regeneration_count,
                        t_start=t_start,
                    )
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="uncited_answer",
                    degraded=False,
                    message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )

        # -----------------------------------------------------------------
        # Step 6: Non-Hallucination Fallback or Auto-Resolution
        # -----------------------------------------------------------------
        if not val_result.is_valid:
            return self._escalate(
                ticket_id=ticket_id,
                queue="human-standard",
                reason="uncited_answer",
                degraded=llm_failed,
                message="To ensure complete accuracy, I've escalated your ticket to our support specialists.",
                tool_calls_count=tool_calls_count,
                retrieval_passes_count=retrieval_passes_count,
                regeneration_count=regeneration_count,
                latency_ms=self._elapsed_ms(t_start),
                traceparent=traceparent,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=message,
            )

        # -----------------------------------------------------------------
        # Step 7: Model Armor Egress Sanitization (REQ-MA-004)
        # -----------------------------------------------------------------
        raw_answer = answer_payload.get("answer", "")
        if self.model_armor_client is not None:
            ma_verdict = self.model_armor_client.sanitize_model_response(raw_answer)
            if ma_verdict.blocked:
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="model_armor_response_blocked",
                    degraded=False,
                    message="To ensure your issue is safely and accurately handled, I have escalated your ticket to our customer support specialists.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )
            if ma_verdict.degraded:
                return self._escalate(
                    ticket_id=ticket_id,
                    queue="human-standard",
                    reason="model_armor_degraded",
                    degraded=True,
                    message="Our system is temporarily experiencing technical delays. A support specialist will follow up with you.",
                    tool_calls_count=tool_calls_count,
                    retrieval_passes_count=retrieval_passes_count,
                    regeneration_count=regeneration_count,
                    latency_ms=self._elapsed_ms(t_start),
                    traceparent=traceparent,
                    customer_id=customer_id,
                    session_id=session_id,
                    body_redacted=message,
                )

        return ResolutionResult(
            status="AUTO_RESOLVED",
            route="resolved",
            answer=raw_answer,
            citations=[c.to_dict() for c in val_result.citations],
            degraded=False,
            tool_calls_count=tool_calls_count,
            retrieval_passes_count=retrieval_passes_count,
            regeneration_count=regeneration_count,
            latency_ms=self._elapsed_ms(t_start),
        )


def create_faq_resolver_agent(settings: Optional[Settings] = None) -> FaqResolverAgent:
    """Build FaqResolverAgent with real production dependencies configured from settings."""
    cfg = settings or Settings.from_env()
    cfg.validate_for_faq()

    # 1. SearchClient with production config
    if cfg.require_real_deps or cfg.agent_search_engine_id:
        from ticket_router.retrieval.search_client import SearchClient
        search_client: Optional[SearchClientProtocol] = SearchClient(
            project=cfg.project,
            location=cfg.agent_search_location,
            engine=cfg.agent_search_engine_id,
        )
    else:
        search_client = None

    # 2. RouterS1Client reranker
    s1_client = RouterS1Client(
        endpoint=cfg.router_s1_endpoint,
        base_url=cfg.router_s1_base_url,
    )
    reranker = VertexLayaReranker(client=s1_client)

    # 3. HttpOrderLookupClient
    if cfg.order_lookup_mcp_url:
        order_client: OrderLookupProtocol = HttpOrderLookupClient(
            base_url=cfg.order_lookup_mcp_url,
        )
    else:
        order_client = FakeOrderLookupClient()

    # 4. Gemini Answer Client
    if cfg.require_real_deps:
        llm_client: LLMClientProtocol = GeminiAnswerClient(
            project=cfg.project,
            location=cfg.location,
            model_role="answer",
        )
    else:
        llm_client = FakeLLMClient()

    # 5. Ticket DB client and PubSub publisher for HumanHandoff
    if cfg.require_real_deps:
        ticket_db_url = cfg.ticket_db_mcp_url or os.environ.get("TICKET_DB_MCP_URL", "")
        ticket_db_client = HttpTicketDbMcpClient(base_url=ticket_db_url)
        topic = os.environ.get("HUMAN_QUEUE_TOPIC", "human-queue")
        publisher = PubSubRestPublisher(
            project=cfg.project or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            topic=topic,
        )
    else:
        ticket_db_client = InMemoryTicketDbMcpClient()
        publisher = FakePublisher()

    human_handoff = HumanHandoff(
        ticket_db_client=ticket_db_client,
        publisher=publisher,
        escalated_by="faq_resolver",
    )

    return FaqResolverAgent(
        search_client=search_client,
        reranker=reranker,
        order_client=order_client,
        llm_client=llm_client,
        circuit_breaker=CircuitBreaker(name="agent_search"),
        human_handoff=human_handoff,
        ticket_db_client=ticket_db_client,
        publisher=publisher,
    )


# ADK wiring: export default root_agent instance (lazy initialization)
root_agent = FaqResolverAgent(lazy_init=True)
