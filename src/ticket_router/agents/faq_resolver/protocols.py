"""protocols.py — Typing protocols and contracts for FaqResolverAgent.

Defines decoupled interfaces for clock, reranker, search, order lookup, and LLM generation.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

# Type alias for a clock function returning monotonic seconds
Clock = Callable[[], float]


@runtime_checkable
class ClockProtocol(Protocol):
    """Monotonic clock protocol for latency measurement and budget enforcement."""

    def __call__(self) -> float:
        ...


@runtime_checkable
class RerankerProtocol(Protocol):
    """Protocol for candidate reranking (e.g. router-s1 choice cross-encoder)."""

    def rerank(
        self,
        query: str,
        candidates: Sequence[Dict[str, Any]],
        top_n: int = 5,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Rerank candidates given a query.

        Args:
            query: The user inquiry or question.
            candidates: Sequence of dicts with at least 'id', 'title', 'content'.
            top_n: Maximum number of reranked candidates to return.

        Returns:
            Tuple of (reranked_candidates_list, latency_ms).
        """
        ...


@runtime_checkable
class OrderLookupProtocol(Protocol):
    """Protocol for order status retrieval with verified session customer binding."""

    def get_order(
        self,
        order_id: str,
        customer_id: str,
        untrusted_customer_id: Optional[str] = None,
        customer_context_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Lookup order by ID for the verified session customer.

        Args:
            order_id: The order identifier.
            customer_id: Verified customer ID from authenticated session context.
            untrusted_customer_id: Untrusted customer ID if passed by tool call / model.
            customer_context_token: Optional signed CCT forwarded unchanged in X-Customer-Context.

        Returns:
            Dict containing order status, items, tracking info, delivery address.
        """
        ...

    def list_orders(
        self,
        customer_id: str,
        limit: int = 5,
        customer_context_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """List recent orders for the verified session customer (REQ-AGENT-007, REQ-AGENT-010).

        Args:
            customer_id: Verified customer ID from authenticated session context.
            limit: Maximum number of orders to return.
            customer_context_token: Optional signed CCT forwarded unchanged in X-Customer-Context.

        Returns:
            Dict containing 'orders' list or None if not found.
        """
        ...


@runtime_checkable
class SearchClientProtocol(Protocol):
    """Protocol for Agent Search client (e.g. Discovery Engine SearchClient)."""

    def search(
        self,
        query: str,
        page_size: int = 10,
        threshold: str = "",
        clean: bool = False,
        intent_family: Optional[str] = None,
        intent_boost: Optional[float] = None,
        fallback_min_results: int = 2,
        ranking_expression: str = "",
        rerank: bool = False,
        token: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute Agent Search query against FAQ data store."""
        ...


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Protocol for grounded answer synthesis with Gemini."""

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
        """Synthesize answer with mandatory citations based strictly on context.

        Returns:
            Dict with 'answer': str, 'citations': list[dict[doc_id, quote]].
        """
        ...
