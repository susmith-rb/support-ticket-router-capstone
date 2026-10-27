"""Hermetic unit tests for the Router-S1 endpoint client.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
"""

import json
from unittest.mock import MagicMock
import pytest

from ticket_router.common.resilience import CircuitBreaker, CircuitBreakerOpenError
from ticket_router.models.router_s1_client import (
    CandidateScore,
    RouterPrediction,
    RouterS1Client,
)


class FakeResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} error")


def test_router_s1_client_url_resolution():
    # REQ-AGENT-002: Default Vertex endpoint URL construction
    client = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
    )
    assert client.predict_url == (
        "https://us-central1-aiplatform.googleapis.com/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1:rawPredict"
    )

    # Custom dedicated DNS base URL
    custom_client = RouterS1Client(
        base_url="https://router-s1.internal.net",
    )
    assert custom_client.predict_url == "https://router-s1.internal.net/predict"


def test_router_s1_client_route_success():
    # REQ-AGENT-002: Router-S1 route classification contract
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({
        "predictions": [
            {
                "escalate": {"label": False, "prob": 0.08, "probabilities": {"false": 0.92, "true": 0.08}},
                "sentiment": {"label": "neutral", "prob": 0.85, "probabilities": {"neutral": 0.85, "angry": 0.15}},
                "urgency": {"label": "normal", "prob": 0.91, "probabilities": {"normal": 0.91, "immediate": 0.09}},
                "intent": {"label": "order_status", "prob": 0.88, "probabilities": {"order_status": 0.88, "other": 0.12}},
                "latency_ms": 42.1,
            }
        ]
    })

    client = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
        session=mock_session,
    )

    pred = client.route("Where is my shipment #12345?")

    assert isinstance(pred, RouterPrediction)
    assert pred.escalate is False
    assert pred.escalate_prob == 0.08
    assert pred.sentiment == "neutral"
    assert pred.urgency == "normal"
    assert pred.intent == "order_status"
    assert pred.intent_prob == 0.88
    assert pred.latency_ms == 42.1
    assert "escalate" in pred.probabilities

    # Verify request payload
    call_args = mock_session.post.call_args
    assert call_args is not None
    posted_data = json.loads(call_args.kwargs["data"].decode("utf-8"))
    assert posted_data["instances"][0]["task"] == "route"
    assert "Where is my shipment" in posted_data["instances"][0]["text"]


def test_router_s1_client_rerank_success():
    # REQ-SERVE-004 / REQ-AGENT-007: Choice reranker candidate scoring
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({
        "predictions": [
            [
                {"id": "doc_1", "score": 0.25, "latency_ms": 15.0},
                {"id": "doc_2", "score": 0.92, "latency_ms": 15.0},
                {"id": "doc_3", "score": 0.65, "latency_ms": 15.0},
            ]
        ]
    })

    client = RouterS1Client(session=mock_session)
    candidates = [
        {"id": "doc_1", "text": "Article 1"},
        {"id": "doc_2", "text": "Article 2"},
        {"id": "doc_3", "text": "Article 3"},
    ]
    ranked = client.rerank(query="refund policy", candidates=candidates)

    assert len(ranked) == 3
    assert all(isinstance(c, CandidateScore) for c in ranked)
    # Output must be sorted descending by score
    assert ranked[0].id == "doc_2"
    assert ranked[0].score == 0.92
    assert ranked[1].id == "doc_3"
    assert ranked[1].score == 0.65
    assert ranked[2].id == "doc_1"
    assert ranked[2].score == 0.25


def test_router_s1_client_circuit_breaker_trips():
    # REQ-AGENT-011: Circuit breaker protection for router-s1 calls
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({}, status_code=503)

    breaker = CircuitBreaker(name="test_s1_breaker", failure_threshold=3, base_cooldown_s=30.0)
    client = RouterS1Client(session=mock_session, breaker=breaker)

    # 3 failures trip the breaker
    for _ in range(3):
        with pytest.raises(RuntimeError):
            client.route("test error")

    assert breaker.state.value == "OPEN"

    # Subsequent call fails immediately without invoking HTTP
    mock_session.post.reset_mock()
    with pytest.raises(CircuitBreakerOpenError):
        client.route("fast fail")

    assert not mock_session.post.called
