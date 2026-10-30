"""Hermetic unit tests for the Router-S1 endpoint client and Laya reranker delegation.

# REQ-AGENT-002: TriageRouter Tiered Decision Policy & Thresholds
# REQ-AGENT-007: FaqResolver Choice Reranker Integration
# REQ-AGENT-011: Distributed Circuit Breakers and Fallback Hierarchies
# REQ-AGENT-012: Execution Budgets, Service Timeouts, and Latency SLAs
# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

import json
import logging
from unittest.mock import MagicMock

import google.auth
import google.auth.credentials
import pytest
import requests
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter

from ticket_router.agents.faq_resolver.reranker import VertexLayaReranker
from ticket_router.common.observability import is_pii_clean
from ticket_router.common.resilience import CircuitBreaker, CircuitBreakerOpenError
from ticket_router.models.router_s1_client import (
    METADATA_TOKEN_URL,
    CandidateScore,
    RouterPrediction,
    RouterS1Client,
)


class FakeResponse:
    """Mock HTTP response object for unit tests."""

    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} error")


@pytest.fixture(autouse=True)
def _mock_adc_token(monkeypatch):
    """Ensure RouterS1Client unit tests remain hermetic without live cloud ADC credentials."""
    fake_creds = MagicMock(spec=google.auth.credentials.Credentials)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **kw: (fake_creds, "susmith-fde-ticket-router"),
    )
    monkeypatch.setattr(
        AuthorizedSession,
        "configure_mtls_channel",
        lambda self, client_cert_callback=None: setattr(self, "_is_mtls", False),
    )
    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.get_access_token",
        lambda *a, **kw: "hermetic-test-token",
    )


def test_router_s1_client_url_resolution():
    """Verify shared regional vs dedicated endpoint URL resolution (REQ-AGENT-002, REQ-AGENT-012)."""
    # 1. Default Vertex endpoint URL construction
    client = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
    )
    assert client.predict_url == (
        "https://us-central1-aiplatform.googleapis.com/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1:rawPredict"
    )

    # 2. Dedicated endpoint URL construction (ROUTER_S1_BASE_URL + ROUTER_S1_ENDPOINT)
    dedicated_client = RouterS1Client(
        base_url="https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog",
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated",
    )
    assert dedicated_client.predict_url == (
        "https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated:rawPredict"
    )

    # 3. Direct predict URL fallback
    direct_client = RouterS1Client(
        base_url="https://router-s1.internal.net/predict",
    )
    assert direct_client.predict_url == "https://router-s1.internal.net/predict"


def test_router_s1_client_connection_reuse():
    """Verify persistent keep-alive session reuse and connection pool configuration (REQ-AGENT-012)."""
    client = RouterS1Client()
    adapter = client.session.adapters.get("https://")
    assert isinstance(adapter, HTTPAdapter)
    assert adapter._pool_connections == 10
    assert adapter._pool_maxsize == 20

    # Verify session instance is maintained across multiple operations
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({
        "predictions": [
            {
                "escalate": {"label": False, "prob": 0.05},
                "sentiment": {"label": "neutral", "prob": 0.9},
                "urgency": {"label": "normal", "prob": 0.95},
                "intent": {"label": "faq", "prob": 0.88},
            }
        ]
    })
    client_pooled = RouterS1Client(session=mock_session)

    # Invoke route then rerank
    client_pooled.route("First inquiry")
    mock_session.post.return_value = FakeResponse({
        "predictions": [[{"id": "doc1", "score": 0.9, "latency_ms": 12.0}]]
    })
    client_pooled.rerank("Query", candidates=[{"id": "doc1"}])

    assert mock_session.post.call_count == 2
    # Verify both calls routed through the exact same session
    assert client_pooled.session is mock_session
    client_pooled.close()


def test_router_s1_client_timeouts():
    """Verify route (0.5s) and rerank (0.6s) default timeouts and overrides (REQ-AGENT-012)."""
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({
        "predictions": [
            {
                "escalate": {"label": False, "prob": 0.05},
                "sentiment": {"label": "neutral", "prob": 0.9},
                "urgency": {"label": "normal", "prob": 0.95},
                "intent": {"label": "faq", "prob": 0.88},
            }
        ]
    })
    client = RouterS1Client(session=mock_session)

    # 1. Default route timeout (0.5 s)
    client.route("Test route default timeout")
    assert mock_session.post.call_args.kwargs["timeout"] == 0.5

    # 2. Explicit route timeout override
    client.route("Test route override", timeout_s=0.25)
    assert mock_session.post.call_args.kwargs["timeout"] == 0.25

    # 3. Default rerank timeout (0.6 s)
    mock_session.post.return_value = FakeResponse({"predictions": [[]]})
    client.rerank("Test query", candidates=[])
    assert mock_session.post.call_args.kwargs["timeout"] == 0.6

    # 4. Explicit rerank timeout override
    client.rerank("Test query override", candidates=[], timeout_s=0.35)
    assert mock_session.post.call_args.kwargs["timeout"] == 0.35


def test_router_s1_client_warm_connection():
    """Verify warm_connection executes health probe safely (REQ-AGENT-012)."""
    mock_session = MagicMock()
    mock_session.get.return_value = FakeResponse({"status": "healthy"}, status_code=200)

    client = RouterS1Client(session=mock_session)
    assert client.warm_connection() is True
    assert mock_session.get.called

    # Failure returns False without raising
    mock_session.get.side_effect = RuntimeError("Network error")
    assert client.warm_connection() is False


def test_router_s1_client_route_success():
    """REQ-AGENT-002: Router-S1 route classification contract."""
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
    """REQ-SERVE-004 / REQ-AGENT-007: Choice reranker candidate scoring."""
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
    """REQ-AGENT-011: Circuit breaker protection for router-s1 calls."""
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


def test_vertex_laya_reranker_delegates_to_router_s1():
    """REQ-AGENT-007: VertexLayaReranker delegates candidate scoring to RouterS1Client."""
    mock_session = MagicMock()
    mock_session.post.return_value = FakeResponse({
        "predictions": [
            [
                {"id": "doc_alpha", "score": 0.40, "latency_ms": 14.5},
                {"id": "doc_beta", "score": 0.95, "latency_ms": 14.5},
            ]
        ]
    })
    s1_client = RouterS1Client(session=mock_session)
    reranker = VertexLayaReranker(client=s1_client)

    candidates = [
        {"id": "doc_alpha", "title": "Alpha Article", "content": "Alpha text"},
        {"id": "doc_beta", "title": "Beta Article", "content": "Beta text"},
    ]
    ranked, latency_ms = reranker.rerank(query="how to return", candidates=candidates, top_n=2)

    assert len(ranked) == 2
    assert ranked[0]["id"] == "doc_beta"
    assert ranked[0]["score"] == 0.95
    assert ranked[1]["id"] == "doc_alpha"
    assert ranked[1]["score"] == 0.40
    assert latency_ms >= 0.0

    # Verify fallback on error
    mock_session.post.side_effect = RuntimeError("Service Unavailable")
    fallback_ranked, fallback_lat = reranker.rerank(query="error query", candidates=candidates, top_n=2)
    assert len(fallback_ranked) == 2
    assert fallback_ranked[0]["id"] == "doc_alpha"
    assert fallback_lat == 0.0


def _route_ok_payload() -> dict:
    return {
        "predictions": [
            {
                "escalate": {"label": False, "prob": 0.07},
                "sentiment": {"label": "neutral", "prob": 0.91},
                "urgency": {"label": "normal", "prob": 0.93},
                "intent": {"label": "faq", "prob": 0.89},
            }
        ]
    }


def _make_http_response(
    request: requests.PreparedRequest, status_code: int, payload: dict
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp.url = request.url or ""
    resp.request = request
    resp.headers["Content-Type"] = "application/json"
    resp._content = json.dumps(payload).encode("utf-8")
    return resp


class _StubCredentials(google.auth.credentials.Credentials):
    """Real google-auth Credentials subclass tracking refresh attempts."""

    def __init__(self, initial_token: str = "bound-tok-initial") -> None:
        super().__init__()
        self.token = initial_token
        self.refresh_calls = 0

    def refresh(self, request) -> None:
        self.refresh_calls += 1
        self.token = f"bound-tok-refreshed-{self.refresh_calls}"


class _StubAdapter(HTTPAdapter):
    """Real HTTPAdapter subclass recording prepared requests without sockets."""

    def __init__(self, handler, **kwargs) -> None:
        super().__init__(**kwargs)
        self._handler = handler
        self.calls: list[tuple[requests.PreparedRequest, object]] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.calls.append((request, timeout))
        return self._handler(request, timeout)


def test_router_s1_dedicated_host_unbound_token_caching_and_401_invalidation(monkeypatch, caplog):
    """REQ-AUTH-006: Dedicated *.prediction.vertexai.goog uses plain pooled session + cached unbound MDS token."""
    now = {"t": 100.0}
    mds_tokens = iter(["unbound-secret-v1", "unbound-secret-v2", "unbound-secret-v3"])
    predict_status = {"code": 200}

    def handle_request(req: requests.PreparedRequest, timeout):
        if req.url and req.url.startswith("http://metadata.google.internal/"):
            assert req.method == "GET"
            assert req.body is None
            assert req.headers.get("Metadata-Flavor") == "Google"
            assert isinstance(timeout, (int, float)) and timeout <= 0.3
            tok = next(mds_tokens)
            return _make_http_response(
                req, 200, {"access_token": tok, "expires_in": 3600, "token_type": "Bearer"}
            )
        if predict_status["code"] != 200:
            return _make_http_response(req, predict_status["code"], {"error": "UNAUTHENTICATED"})
        return _make_http_response(req, 200, _route_ok_payload())

    stub_adapter = _StubAdapter(handle_request, pool_connections=10, pool_maxsize=20, max_retries=0)
    plain_session = requests.Session()
    plain_session.mount("https://", stub_adapter)
    plain_session.mount("http://", stub_adapter)

    discarded_mtls_session = MagicMock(spec=AuthorizedSession)
    discarded_mtls_session.is_mtls = True
    session_create_kwargs: list[dict] = []

    def fake_create_google_session(**kwargs):
        session_create_kwargs.append(kwargs)
        return discarded_mtls_session, True

    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.create_google_session",
        fake_create_google_session,
    )
    monkeypatch.setattr(
        "ticket_router.models.router_s1_client._create_default_session",
        lambda: plain_session,
    )

    breaker = CircuitBreaker(name="test_s1_unbound", failure_threshold=3, base_cooldown_s=30.0)
    client = RouterS1Client(
        base_url="https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog",
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated",
        breaker=breaker,
        clock=lambda: now["t"],
    )

    assert client.predict_url == (
        "https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated:rawPredict"
    )

    # 1. Initial call: mTLS session closed, plain pooled session selected, MDS GET + predict POST
    pred = client.route("Where is my shipment?")
    assert pred.intent == "faq"
    assert client.is_mtls is True
    assert client.session is plain_session
    assert not isinstance(client.session, AuthorizedSession)
    discarded_mtls_session.close.assert_called_once()
    assert session_create_kwargs[-1] == {
        "pool_connections": 10,
        "pool_maxsize": 20,
        "max_retries": 0,
        "max_refresh_attempts": 0,
    }
    mds_calls = [c for c in stub_adapter.calls if c[0].url == METADATA_TOKEN_URL]
    post_calls = [c for c in stub_adapter.calls if c[0].method == "POST"]
    assert len(mds_calls) == 1
    assert len(post_calls) == 1
    assert post_calls[-1][0].headers["Authorization"] == "Bearer unbound-secret-v1"

    # 2. Cached before (expires_in - 60 s) = 3540 s -> no extra MDS GET
    now["t"] = 100.0 + 3539.0
    client.route("Follow-up question")
    mds_calls = [c for c in stub_adapter.calls if c[0].url == METADATA_TOKEN_URL]
    post_calls = [c for c in stub_adapter.calls if c[0].method == "POST"]
    assert len(mds_calls) == 1
    assert len(post_calls) == 2
    assert post_calls[-1][0].headers["Authorization"] == "Bearer unbound-secret-v1"

    # 3. Past (expires_in - 60 s) = 3541 s -> refreshes to unbound-secret-v2
    now["t"] = 100.0 + 3541.0
    client.route("After expiry window")
    mds_calls = [c for c in stub_adapter.calls if c[0].url == METADATA_TOKEN_URL]
    post_calls = [c for c in stub_adapter.calls if c[0].method == "POST"]
    assert len(mds_calls) == 2
    assert post_calls[-1][0].headers["Authorization"] == "Bearer unbound-secret-v2"

    # 4. HTTP 401 on unbound path -> 1 POST only, invalidates cached token, logs PII-free diagnostic
    now["t"] += 1.0
    predict_status["code"] = 401
    posts_before = len([c for c in stub_adapter.calls if c[0].method == "POST"])
    with caplog.at_level(logging.WARNING, logger="ticket_router.models.router_s1_client"):
        with pytest.raises(requests.HTTPError):
            client.route("Customer alice@example.com secret body")
    posts_after = len([c for c in stub_adapter.calls if c[0].method == "POST"])
    assert posts_after - posts_before == 1  # No retry loop inside 0.5 s budget

    warnings = [r for r in caplog.records if "router_s1_mtls_unauthorized" in r.getMessage()]
    assert len(warnings) == 1
    w_msg = warnings[0].getMessage()
    assert "unbound-secret-v2" not in w_msg
    assert "alice@example.com" not in w_msg
    assert is_pii_clean({
        "event": getattr(warnings[0], "event", ""),
        "status_code": getattr(warnings[0], "status_code", 0),
        "target_host": getattr(warnings[0], "target_host", ""),
        "endpoint_resource": getattr(warnings[0], "endpoint_resource", ""),
        "is_mtls": getattr(warnings[0], "is_mtls", False),
        "auth_mode": getattr(warnings[0], "auth_mode", ""),
    })

    # 5. Next call after 401 fetches fresh unbound-secret-v3 because cache was invalidated
    predict_status["code"] = 200
    now["t"] += 1.0
    client.route("Recovered call")
    mds_calls = [c for c in stub_adapter.calls if c[0].url == METADATA_TOKEN_URL]
    post_calls = [c for c in stub_adapter.calls if c[0].method == "POST"]
    assert len(mds_calls) == 3
    assert post_calls[-1][0].headers["Authorization"] == "Bearer unbound-secret-v3"


def test_router_s1_googleapis_mtls_and_sa_runtime_paths(monkeypatch):
    """REQ-AUTH-006: *.googleapis.com uses mTLS session + rewrite; SA runtime (is_mtls=False) unchanged."""
    # 1. *.googleapis.com with is_mtls=True -> mTLS session kept, host rewritten to .mtls.googleapis.com
    mock_mtls_session = MagicMock(spec=AuthorizedSession)
    mock_mtls_session.is_mtls = True
    mock_mtls_session.post.return_value = FakeResponse(_route_ok_payload(), status_code=200)
    mock_mtls_session.get.return_value = FakeResponse({"status": "ok"}, status_code=200)

    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.create_google_session",
        lambda **kw: (mock_mtls_session, True),
    )
    shared_client = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
    )
    assert shared_client.is_mtls is True
    assert shared_client.predict_url == (
        "https://us-central1-aiplatform.mtls.googleapis.com/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1:rawPredict"
    )
    assert shared_client.warm_connection() is True
    assert mock_mtls_session.get.call_args.args[0] == (
        "https://us-central1-aiplatform.mtls.googleapis.com/healthz"
    )
    shared_client.route("Password reset")
    assert mock_mtls_session.post.call_args.args[0] == shared_client.predict_url
    assert "Authorization" not in mock_mtls_session.post.call_args.kwargs["headers"]
    mock_mtls_session.close.assert_not_called()

    # 2. SA runtime (is_mtls=False) -> unchanged on BOTH shared and dedicated hosts (no MDS fallback)
    mock_sa_session = MagicMock(spec=AuthorizedSession)
    mock_sa_session.is_mtls = False
    mock_sa_session.post.return_value = FakeResponse(_route_ok_payload(), status_code=200)

    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.create_google_session",
        lambda **kw: (mock_sa_session, False),
    )
    sa_shared = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
    )
    assert sa_shared.is_mtls is False
    assert sa_shared.predict_url == (
        "https://us-central1-aiplatform.googleapis.com/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1:rawPredict"
    )
    sa_shared.route("SA shared call")

    sa_dedicated = RouterS1Client(
        base_url="https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog",
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated",
    )
    assert sa_dedicated.is_mtls is False
    assert sa_dedicated.session is mock_sa_session
    sa_dedicated.route("SA dedicated call")
    assert mock_sa_session.post.call_count == 2
    assert "Authorization" not in mock_sa_session.post.call_args.kwargs["headers"]

    # 3. Injected session + token_provider -> bypasses create_google_session
    injected = MagicMock()
    injected.is_mtls = False
    injected.post.return_value = FakeResponse(_route_ok_payload(), status_code=200)
    custom = RouterS1Client(session=injected, token_provider=lambda: "injected-tok")
    custom.route("Custom")
    assert injected.post.call_args.kwargs["headers"]["Authorization"] == "Bearer injected-tok"


def test_router_s1_real_authorized_session_401_makes_single_post(monkeypatch):
    """REQ-AUTH-006: Real AuthorizedSession with max_refresh_attempts=0 sends exactly 1 POST on 401."""
    creds = _StubCredentials("bound-token-1")
    monkeypatch.setattr(google.auth, "default", lambda *a, **kw: (creds, "susmith-fde-ticket-router"))

    adapter = _StubAdapter(
        lambda req, timeout: _make_http_response(req, 401, {"error": "UNAUTHENTICATED"}),
        pool_connections=10,
        pool_maxsize=20,
        max_retries=0,
    )

    def fake_configure_mtls(self: AuthorizedSession, client_cert_callback=None) -> None:
        self._is_mtls = True
        self.mount("https://", adapter)

    monkeypatch.setattr(AuthorizedSession, "configure_mtls_channel", fake_configure_mtls)

    breaker = CircuitBreaker(name="test_real_auth_401", failure_threshold=2, base_cooldown_s=30.0)
    client = RouterS1Client(
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1",
        breaker=breaker,
    )

    with pytest.raises(requests.HTTPError):
        client.route("Single post on 401")

    assert isinstance(client.session, AuthorizedSession)
    assert len(adapter.calls) == 1  # Strictly 1 POST, NOT 3
    assert creds.refresh_calls == 0  # Zero MDS refresh retries
    assert adapter.calls[0][0].url == (
        "https://us-central1-aiplatform.mtls.googleapis.com/v1/"
        "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1:rawPredict"
    )

    # Second 401 trips circuit breaker cleanly after 1 more POST
    with pytest.raises(requests.HTTPError):
        client.route("Second 401")
    assert len(adapter.calls) == 2
    assert breaker.state.value == "OPEN"


def test_router_s1_default_credentials_error_not_cached_and_counted_by_breaker(monkeypatch):
    """REQ-AUTH-006: DefaultCredentialsError propagates into breaker without caching a broken session."""
    mock_ok_session = MagicMock(spec=AuthorizedSession)
    mock_ok_session.is_mtls = False
    mock_ok_session.post.return_value = FakeResponse(_route_ok_payload(), status_code=200)

    attempts = {"n": 0}

    def flaky_create_google_session(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DefaultCredentialsError("transient metadata timeout")
        return mock_ok_session, False

    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.create_google_session",
        flaky_create_google_session,
    )

    breaker = CircuitBreaker(name="test_adc_retry", failure_threshold=3, base_cooldown_s=30.0)
    client = RouterS1Client(breaker=breaker)

    # 1st call fails into breaker and does NOT cache _session
    with pytest.raises(DefaultCredentialsError, match="transient metadata timeout"):
        client.route("Cold start ticket")
    assert client._session is None
    assert breaker.consecutive_failures == 1

    # 2nd call retries create_google_session, succeeds, and caches valid session
    pred = client.route("Next ticket")
    assert pred.intent == "faq"
    assert attempts["n"] == 2
    assert client._session is mock_ok_session
    assert breaker.consecutive_failures == 0


def test_router_s1_unbound_token_blocked_for_http_and_non_allowlisted_hosts(monkeypatch):
    """REQ-AUTH-006: Unbound MDS token MUST NOT be sent to http:// or non-*.prediction.vertexai.goog hosts."""
    mds_called = {"count": 0}

    def forbid_default_session():
        mds_called["count"] += 1
        raise AssertionError("Plain unbound-token session must not be created for non-allowlisted URL")

    monkeypatch.setattr(
        "ticket_router.models.router_s1_client._create_default_session",
        forbid_default_session,
    )

    def make_mtls_session():
        sess = MagicMock(spec=AuthorizedSession)
        sess.is_mtls = True
        sess.post.return_value = FakeResponse(_route_ok_payload(), status_code=200)
        return sess

    # 1. Plaintext http:// on dedicated vertexai.goog host -> keeps mTLS AuthorizedSession, NO unbound token
    mtls_http_ded = make_mtls_session()
    monkeypatch.setattr(
        "ticket_router.models.router_s1_client.create_google_session",
        lambda **kw: (mtls_http_ded, True),
    )
    http_client = RouterS1Client(
        base_url="http://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog",
        endpoint="projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated",
    )
    http_client.route("Test over http")
    assert http_client.session is mtls_http_ded
    assert http_client._uses_unbound_mds_token is False
    mtls_http_ded.close.assert_not_called()
    assert "Authorization" not in mtls_http_ded.post.call_args.kwargs["headers"]
    assert mds_called["count"] == 0

    # 2. Non-allowlisted https:// host (including suffix-spoofed domain) -> keeps mTLS AuthorizedSession
    for non_allowlisted_url in (
        "https://router-s1.internal.net/predict",
        "https://evilprediction.vertexai.goog/v1/projects/p/locations/us-central1/endpoints/e:rawPredict",
        "https://127.0.0.1:8443/predict",
    ):
        mtls_other = make_mtls_session()
        monkeypatch.setattr(
            "ticket_router.models.router_s1_client.create_google_session",
            lambda _s=mtls_other, **kw: (_s, True),
        )
        other_client = RouterS1Client(base_url=non_allowlisted_url)
        other_client.route("Test non-allowlisted https")
        assert other_client.session is mtls_other
        assert other_client._uses_unbound_mds_token is False
        mtls_other.close.assert_not_called()
        assert "Authorization" not in mtls_other.post.call_args.kwargs["headers"]
        assert mds_called["count"] == 0
