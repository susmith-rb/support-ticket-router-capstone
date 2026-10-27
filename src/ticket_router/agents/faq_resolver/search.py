"""search.py — Production Agent Search integration for FaqResolverAgent.

REQ-AGENT-007, REQ-RAG-RETR-008, REQ-RAG-RETR-010.
Queries Agent Search (Discovery Engine) using the production configuration
'intent_soft_noisy_0.2' (threshold=LOWEST, clean=True, soft intent boost=0.2,
page_size=10, fallback_min_results=2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional

from ticket_router.agents.faq_resolver.protocols import SearchClientProtocol
from ticket_router.common.resilience import (
    AGENT_SEARCH_TIMEOUT_S,
    CircuitBreaker,
    CircuitBreakerOpenError,
)

PRODUCTION_CONFIG_NAME: str = "intent_soft_noisy_0.2"


def build_production_search_kwargs(
    query: Optional[str] = None,
    intent_family: Optional[str] = None,
    token: Optional[str] = None,
    query_item: Optional[Dict[str, Any]] = None,
    noisy_intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct search keyword arguments matching production config intent_soft_noisy_0.2.

    Replicates evals.ablation_eval.build_search_kwargs without importing offline evals/.
    Compatible with both runtime invocation (query, intent_family) and eval harness
    (query_item, noisy_intent).
    """
    if query_item is not None:
        qtext = query_item.get("query") or query_item.get("text") or ""
        active_intent = noisy_intent
    else:
        qtext = query or ""
        active_intent = intent_family

    return {
        "query": qtext,
        "page_size": 10,
        "threshold": "LOWEST",
        "clean": True,
        "intent_family": active_intent,
        "intent_boost": 0.2,
        "fallback_min_results": 2,
        "ranking_expression": "",
        "rerank": False,
        "token": token,
    }


@dataclass
class RetrievedDocument:
    """Standardized retrieved document representation."""

    id: str
    title: str
    content: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchExecutionResult:
    """Execution outcome of an Agent Search query."""

    success: bool
    document_ids: List[str] = field(default_factory=list)
    documents: Dict[str, RetrievedDocument] = field(default_factory=dict)
    latency_ms: float = 0.0
    degraded: bool = False
    error: Optional[str] = None


def execute_faq_search(
    client: SearchClientProtocol,
    query: str,
    intent_family: Optional[str] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    token: Optional[str] = None,
) -> SearchExecutionResult:
    """Execute FAQ document retrieval under circuit breaker and timeout budget.

    Args:
        client: Agent Search client implementing SearchClientProtocol.
        query: User search query.
        intent_family: Predicted intent from router / context.
        circuit_breaker: Optional CircuitBreaker instance.
        token: Optional OAuth Bearer token.

    Returns:
        SearchExecutionResult with documents or degraded fallback indicator.
    """
    kwargs = build_production_search_kwargs(
        query=query,
        intent_family=intent_family,
        token=token,
    )

    t0 = time.perf_counter()

    def _call_search() -> Any:
        return client.search(**kwargs)

    try:
        if circuit_breaker is not None:
            raw_res = circuit_breaker.call_with_timeout(
                _call_search,
                timeout_s=AGENT_SEARCH_TIMEOUT_S,
            )
        else:
            raw_res = _call_search()

        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Parse SearchResult object or dict
        if hasattr(raw_res, "document_ids"):
            doc_ids = list(raw_res.document_ids)
            raw_results = getattr(raw_res, "raw_results", [])
        elif isinstance(raw_res, dict):
            doc_ids = list(raw_res.get("document_ids", []))
            raw_results = raw_res.get("raw_results", [])
        else:
            doc_ids = []
            raw_results = []

        documents: Dict[str, RetrievedDocument] = {}
        for r in raw_results:
            doc = r.get("document", {}) if isinstance(r, dict) else {}
            d_id = doc.get("id") or r.get("id")
            if not d_id:
                continue

            derived = doc.get("derivedStructData", {}) if isinstance(doc, dict) else {}
            struct = doc.get("structData", {}) if isinstance(doc, dict) else {}
            title = derived.get("title") or struct.get("title") or d_id
            snippets = derived.get("snippets", [])
            snippet_text = snippets[0].get("snippet", "") if snippets else ""
            content = struct.get("content") or snippet_text or title

            documents[d_id] = RetrievedDocument(
                id=d_id,
                title=title,
                content=content,
                raw=doc,
            )

        # Fallback if raw_results was empty but document_ids exists (e.g. from fakes)
        for d_id in doc_ids:
            if d_id not in documents:
                documents[d_id] = RetrievedDocument(
                    id=d_id,
                    title=d_id,
                    content="",
                )

        return SearchExecutionResult(
            success=True,
            document_ids=doc_ids,
            documents=documents,
            latency_ms=round(latency_ms, 2),
            degraded=False,
        )

    except (CircuitBreakerOpenError, Exception) as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return SearchExecutionResult(
            success=False,
            document_ids=[],
            documents={},
            latency_ms=round(latency_ms, 2),
            degraded=True,
            error=str(exc),
        )
