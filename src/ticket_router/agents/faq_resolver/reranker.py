"""reranker.py — Cross-encoder candidate scoring against router-s1 for FaqResolverAgent.

REQ-AGENT-007, REQ-SERVE-004.
Performs cross-encoder candidate scoring against the router-s1 Vertex Prediction endpoint
using the 'task: rerank' contract, sorting candidates descending by score.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import urllib.error
import urllib.request

from ticket_router.agents.faq_resolver.protocols import RerankerProtocol
from ticket_router.common.auth import get_access_token
from ticket_router.common.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    LAYA_RERANK_TIMEOUT_S,
)


class VertexLayaReranker(RerankerProtocol):
    """Production reranker client connecting to router-s1 Vertex Prediction Endpoint."""

    def __init__(
        self,
        endpoint_url: str,
        project: str = "susmith-fde-ticket-router",
        location: str = "us-central1",
        timeout_s: float = LAYA_RERANK_TIMEOUT_S,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.project = project
        self.location = location
        self.timeout_s = timeout_s
        self.circuit_breaker = circuit_breaker

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
        body = {
            "instances": [
                {
                    "task": "rerank",
                    "query": query,
                    "candidates": cand_list,
                    "top_n": top_n,
                }
            ]
        }

        def _do_http() -> Tuple[List[Dict[str, Any]], float]:
            token = get_access_token()
            req = urllib.request.Request(
                self.endpoint_url,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            lat_ms = (time.perf_counter() - t0) * 1000.0

            predictions = data.get("predictions", [])
            if predictions and isinstance(predictions[0], list):
                return predictions[0][:top_n], lat_ms
            elif predictions and isinstance(predictions[0], dict):
                ranked = predictions[0].get("ranked_candidates", [])
                return ranked[:top_n], lat_ms
            return cand_list[:top_n], lat_ms

        try:
            if self.circuit_breaker is not None:
                return self.circuit_breaker.call_with_timeout(_do_http, timeout_s=self.timeout_s)
            return _do_http()
        except (CircuitBreakerOpenError, Exception):
            # In case reranking fails, gracefully fall back to original candidates
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
