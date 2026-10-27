"""reranker.py — Cross-encoder candidate scoring against router-s1 for FaqResolverAgent.

REQ-AGENT-007, REQ-SERVE-004.
Performs cross-encoder candidate scoring against the router-s1 Vertex Prediction endpoint
using the 'task: rerank' contract, sorting candidates descending by score.
Delegates to RouterS1Client for persistent keep-alive connection pooling and circuit breaking.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ticket_router.agents.faq_resolver.protocols import RerankerProtocol
from ticket_router.common.resilience import (
    LAYA_RERANK_TIMEOUT_S,
    CircuitBreaker,
    CircuitBreakerOpenError,
)
from ticket_router.models.router_s1_client import RouterS1Client


class VertexLayaReranker(RerankerProtocol):
    """Production reranker client connecting to router-s1 Vertex Prediction Endpoint."""

    def __init__(
        self,
        endpoint_url: str = "",
        project: str = "susmith-fde-ticket-router",
        location: str = "us-central1",
        timeout_s: float = LAYA_RERANK_TIMEOUT_S,
        circuit_breaker: Optional[CircuitBreaker] = None,
        client: Optional[RouterS1Client] = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.project = project
        self.location = location
        self.timeout_s = timeout_s
        self.circuit_breaker = circuit_breaker
        if client is not None:
            self._client = client
        else:
            base_url = (
                endpoint_url
                if (endpoint_url and "://" in endpoint_url and "projects/" not in endpoint_url)
                else None
            )
            endpoint = endpoint_url if (endpoint_url and "projects/" in endpoint_url) else None
            self._client = RouterS1Client(
                endpoint=endpoint,
                base_url=base_url,
                rerank_timeout_s=timeout_s,
                breaker=circuit_breaker,
            )

    def rerank(
        self,
        query: str,
        candidates: Sequence[Dict[str, Any]],
        top_n: int = 5,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Call Vertex endpoint with task='rerank' to score and sort candidates."""
        if not candidates:
            return [], 0.0

        cand_list = [dict(c) for c in candidates]
        t0 = time.perf_counter()
        try:
            scores = self._client.rerank(
                query=query,
                candidates=cand_list,
                timeout_s=self.timeout_s,
            )
            lat_ms = (time.perf_counter() - t0) * 1000.0

            by_id = {str(c.get("id", "")): dict(c) for c in cand_list}
            ranked: List[Dict[str, Any]] = []
            seen_ids = set()
            for s in scores:
                if s.id in by_id and s.id not in seen_ids:
                    c = by_id[s.id]
                    c["score"] = s.score
                    ranked.append(c)
                    seen_ids.add(s.id)

            # Preserve any candidates omitted by reranker
            for c in cand_list:
                cid = str(c.get("id", ""))
                if cid not in seen_ids:
                    ranked.append(c)
                    seen_ids.add(cid)

            return ranked[:top_n], lat_ms
        except (CircuitBreakerOpenError, Exception):
            # Graceful fallback to unranked candidates per REQ-AGENT-011
            return cand_list[:top_n], 0.0


class FakeReranker(RerankerProtocol):
    """Deterministic in-memory fake reranker for unit and integration testing."""

    def __init__(
        self,
        preferred_id: Optional[str] = None,
        latency_ms: float = 15.0,
        fail_with: Optional[Exception] = None,
    ) -> None:
        self.preferred_id = preferred_id
        self.latency_ms = latency_ms
        self.fail_with = fail_with
        self.call_history: List[Dict[str, Any]] = []

    def rerank(
        self,
        query: str,
        candidates: Sequence[Dict[str, Any]],
        top_n: int = 5,
    ) -> Tuple[List[Dict[str, Any]], float]:
        self.call_history.append({"query": query, "candidates": list(candidates), "top_n": top_n})
        if self.fail_with is not None:
            raise self.fail_with

        cand_list = [dict(c) for c in candidates]
        if self.preferred_id:
            # Promote preferred_id to rank 1
            preferred = [c for c in cand_list if c.get("id") == self.preferred_id]
            others = [c for c in cand_list if c.get("id") != self.preferred_id]
            cand_list = preferred + others

        # Assign synthetic descending scores
        for idx, c in enumerate(cand_list):
            c["score"] = round(1.0 - (idx * 0.1), 3)

        return cand_list[:top_n], self.latency_ms
