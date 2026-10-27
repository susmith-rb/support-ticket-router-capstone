"""Router-S1 Vertex AI Prediction Endpoint Client.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any, Dict, List, Optional

from ticket_router.common.auth import get_access_token
from ticket_router.common.resilience import (
    ROUTER_S1_TIMEOUT_S,
    CircuitBreaker,
)

DEFAULT_ENDPOINT_RESOURCE = "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1"


@dataclass(frozen=True)
class CandidateScore:
    """Reranker score for a knowledge-base article candidate."""
    id: str
    score: float
    latency_ms: float = 0.0


@dataclass(frozen=True)
class RouterPrediction:
    """Typed multi-head prediction returned by the router-s1 encoder."""
    escalate: bool
    escalate_prob: float
    sentiment: str
    sentiment_prob: float
    urgency: str
    urgency_prob: float
    intent: str
    intent_prob: float
    probabilities: Dict[str, Dict[str, float]] = field(default_factory=dict)
    latency_ms: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


class RouterS1Client:
    """Client for Vertex AI prediction endpoint serving the fine-tuned router-s1 encoder.

    Follows AGENTS.md rule 4: calls :rawPredict on Vertex endpoints serving Laya.
    Protected by CircuitBreaker (REQ-AGENT-011) with strict timeout budget (REQ-AGENT-012).
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: float = ROUTER_S1_TIMEOUT_S,
        breaker: Optional[CircuitBreaker] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.endpoint_resource = endpoint or os.environ.get("ROUTER_S1_ENDPOINT", DEFAULT_ENDPOINT_RESOURCE)
        self.base_url = base_url or os.environ.get("ROUTER_S1_BASE_URL")
        self.timeout_s = timeout_s
        self.breaker = breaker or CircuitBreaker(name="router_s1")
        self._session = session

        # Resolve prediction URL
        self.predict_url = self._resolve_predict_url()

    def _resolve_predict_url(self) -> str:
        """Resolve full target URL using base_url or Vertex endpoint resource."""
        if self.base_url:
            cleaned = self.base_url.rstrip("/")
            if cleaned.endswith(":rawPredict") or cleaned.endswith(":predict") or cleaned.endswith("/predict"):
                return cleaned
            return f"{cleaned}/predict"

        # Vertex AI endpoint format: projects/{project}/locations/{location}/endpoints/{endpoint_id}
        match = re.match(
            r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/endpoints/(?P<endpoint>[^/]+)$",
            self.endpoint_resource,
        )
        if match:
            location = match.group("location")
            return f"https://{location}-aiplatform.googleapis.com/v1/{self.endpoint_resource}:rawPredict"

        # Fallback if bare endpoint ID or relative path
        return f"https://us-central1-aiplatform.googleapis.com/v1/{self.endpoint_resource}:rawPredict"

    def _get_headers(self) -> Dict[str, str]:
        """Construct request headers including Google Cloud ADC bearer token."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ticket-router/router-s1-client",
        }
        token = get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _execute_request(self, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        """Send HTTP POST request to endpoint."""
        data_bytes = json.dumps(payload).encode("utf-8")
        headers = self._get_headers()

        if self._session is not None:
            resp = self._session.post(
                self.predict_url,
                data=data_bytes,
                headers=headers,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            return resp.json()

        import requests
        resp = requests.post(
            self.predict_url,
            data=data_bytes,
            headers=headers,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    def _post_http(self, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        """Execute raw HTTP POST, wrapped by CircuitBreaker."""
        return self.breaker.call(self._execute_request, payload, timeout_s)

    def route(self, text: str, timeout_s: Optional[float] = None) -> RouterPrediction:
        """Evaluate text via router-s1 encoder heads (REQ-AGENT-002)."""
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        payload = {
            "instances": [
                {
                    "task": "route",
                    "text": text,
                }
            ]
        }
        resp = self._post_http(payload, timeout_s=effective_timeout)
        return self._parse_route_response(resp)

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> List[CandidateScore]:
        """Score and rank candidates against query using choice reranker."""
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        payload = {
            "instances": [
                {
                    "task": "rerank",
                    "query": query,
                    "candidates": candidates,
                }
            ]
        }
        resp = self._post_http(payload, timeout_s=effective_timeout)
        return self._parse_rerank_response(resp)

    def _parse_route_response(self, resp: Dict[str, Any]) -> RouterPrediction:
        """Parse raw prediction JSON into a strongly typed RouterPrediction."""
        predictions = resp.get("predictions", [])
        if not predictions:
            if "escalate" in resp:
                pred = resp
            else:
                raise ValueError(f"Invalid route prediction response format: {resp}")
        else:
            pred = predictions[0]

        esc_head = pred.get("escalate", {})
        sent_head = pred.get("sentiment", {})
        urg_head = pred.get("urgency", {})
        intent_head = pred.get("intent", {})

        # Extract escalation decision
        esc_label = esc_head.get("label", False)
        if isinstance(esc_label, str):
            esc_bool = esc_label.lower() in ("true", "yes", "1", "escalate")
        else:
            esc_bool = bool(esc_label)

        esc_prob = float(esc_head.get("prob", 1.0 if esc_bool else 0.0))
        esc_probs = {str(k): float(v) for k, v in esc_head.get("probabilities", {}).items()}

        # Extract sentiment
        sent_label = str(sent_head.get("label", "neutral"))
        sent_prob = float(sent_head.get("prob", 0.0))
        sent_probs = {str(k): float(v) for k, v in sent_head.get("probabilities", {}).items()}

        # Extract urgency
        urg_label = str(urg_head.get("label", "normal"))
        urg_prob = float(urg_head.get("prob", 0.0))
        urg_probs = {str(k): float(v) for k, v in urg_head.get("probabilities", {}).items()}

        # Extract intent
        intent_label = str(intent_head.get("label", "other"))
        intent_prob = float(intent_head.get("prob", 0.0))
        intent_probs = {str(k): float(v) for k, v in intent_head.get("probabilities", {}).items()}

        latency_ms = float(pred.get("latency_ms", resp.get("latency_ms", 0.0)))

        all_probs = {
            "escalate": esc_probs,
            "sentiment": sent_probs,
            "urgency": urg_probs,
            "intent": intent_probs,
        }

        return RouterPrediction(
            escalate=esc_bool,
            escalate_prob=esc_prob,
            sentiment=sent_label,
            sentiment_prob=sent_prob,
            urgency=urg_label,
            urgency_prob=urg_prob,
            intent=intent_label,
            intent_prob=intent_prob,
            probabilities=all_probs,
            latency_ms=latency_ms,
            raw=pred,
        )

    def _parse_rerank_response(self, resp: Dict[str, Any]) -> List[CandidateScore]:
        """Parse raw rerank prediction JSON into sorted CandidateScore list."""
        predictions = resp.get("predictions", [])
        if not predictions:
            if isinstance(resp, list):
                items = resp
            else:
                items = []
        else:
            items = predictions[0] if isinstance(predictions[0], list) else predictions

        scores: List[CandidateScore] = []
        for item in items:
            c_id = str(item.get("id", ""))
            score = float(item.get("score", 0.0))
            lat = float(item.get("latency_ms", 0.0))
            scores.append(CandidateScore(id=c_id, score=score, latency_ms=lat))

        # Ensure sorted descending by score
        scores.sort(key=lambda x: x.score, reverse=True)
        return scores
