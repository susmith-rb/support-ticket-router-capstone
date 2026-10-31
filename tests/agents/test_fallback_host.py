"""Unit and integration tests for Cloud Run fallback agent host (triage-fallback).

# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)
# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-IDENT-007: Defense-in-Depth Against Caller-Asserted Identity
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from ticket_router.agents.fallback_host import InMemorySessionStore, create_app
from ticket_router.agents.fallback_host.env_contract import (
    filter_reserved_env_vars,
    get_triage_env_contract,
)
from ticket_router.common.settings import Settings
from ticket_router.serve import get_app


class FakeAdkAgent:
    """Mock ADK agent satisfying TriageRouterAdkAgent query interface."""

    def __init__(self) -> None:
        self.last_query_kwargs = {}

    def query(
        self,
        message: str,
        session_id: str = "default_session",
        customer_id_token: str | None = None,
        oauth_access_token: str | None = None,
        **kwargs,
    ) -> dict:
        self.last_query_kwargs = {
            "message": message,
            "session_id": session_id,
            "customer_id_token": customer_id_token,
            "oauth_access_token": oauth_access_token,
            **kwargs,
        }
        return {
            "ticket_id": f"TICK-{session_id[:8]}",
            "session_id": session_id,
            "decision": "auto_rag",
            "queue": "automated",
            "calming_ack": None,
            "intent": "shipping_status",
            "answer": f"Echo: {message}",
            "latency_ms": 15.5,
            "status_code": 200,
        }


# REQ-AGENT-035
def test_serve_py_routes_to_fallback_app():
    """Verify serve.py resolves triage_fallback and instruments FastAPI app."""
    app = get_app("triage_fallback")
    assert app is not None
    assert getattr(app.state, "_tracing_instrumented", False) is True

    # Test alternate aliases
    app_alias = get_app("triage_router")
    assert app_alias is not None
    app_alias2 = get_app("fallback_host")
    assert app_alias2 is not None


# REQ-AGENT-035
def test_fallback_health_endpoint_documents_degraded_state():
    """Verify /health and /healthz endpoints return 200 and document degraded session & memory bank."""
    fake_agent = FakeAdkAgent()
    store = InMemorySessionStore()
    app = create_app(agent=fake_agent, session_store=store)
    client = TestClient(app)

    for path in ("/health", "/healthz"):
        resp = client.get(path)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "triage-fallback"
        assert data["runtime"] == "cloud-run"
        assert "degraded_mode" in data
        assert data["degraded_mode"]["sessions"] == "degraded_in_memory"
        assert data["degraded_mode"]["memory_bank"] == "unavailable"


# REQ-AGENT-035
def test_fallback_app_delegates_to_shared_agent_factory():
    """Verify triage query delegates directly to shared TriageRouter agent factory without duplicate logic."""
    fake_agent = FakeAdkAgent()
    store = InMemorySessionStore()
    app = create_app(agent=fake_agent, session_store=store)
    client = TestClient(app)

    payload = {
        "message": "Where is my package?",
        "session_id": "sess-test-42",
        "customer_id_token": "mock.jwt.token",
    }
    resp = client.post("/query", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["ticket_id"] == "TICK-sess-tes"
    assert data["session_id"] == "sess-test-42"
    assert data["decision"] == "auto_rag"
    assert data["queue"] == "automated"
    assert data["answer"] == "Echo: Where is my package?"
    assert data["reply"] == "Echo: Where is my package?"
    assert data["runtime"] == "cloud-run-fallback"
    assert data["degraded_mode"]["sessions"] == "degraded_in_memory"

    # Verify kwargs received by agent factory
    assert fake_agent.last_query_kwargs["message"] == "Where is my package?"
    assert fake_agent.last_query_kwargs["session_id"] == "sess-test-42"
    assert fake_agent.last_query_kwargs["customer_id_token"] == "mock.jwt.token"


# REQ-AGENT-035
def test_fallback_app_in_memory_sessions():
    """Verify session management is in-memory and Cloud SQL-free."""
    fake_agent = FakeAdkAgent()
    store = InMemorySessionStore()
    app = create_app(agent=fake_agent, session_store=store)
    client = TestClient(app)

    # 1. Create session via POST /sessions
    sess_payload = {
        "session_id": "custom-sess-100",
        "user_id": "cust_1001",
        "state": {"customer_id_token": "initial-token"},
    }
    create_resp = client.post("/sessions", json=sess_payload)
    assert create_resp.status_code == 201
    created_data = create_resp.json()
    assert created_data["session_id"] == "custom-sess-100"
    assert created_data["state"]["customer_id_token"] == "initial-token"
    assert created_data["degraded"] is True

    # 2. Query reusing session picks up state token
    query_resp = client.post(
        "/query",
        json={"message": "Status check", "session_id": "custom-sess-100"},
    )
    assert query_resp.status_code == 200
    assert fake_agent.last_query_kwargs["customer_id_token"] == "initial-token"

    # 3. Retrieve session via GET /sessions/{id}
    get_resp = client.get("/sessions/custom-sess-100")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == "custom-sess-100"

    # 4. Delete session via DELETE /sessions/{id}
    del_resp = client.delete("/sessions/custom-sess-100")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # 5. Non-existent session returns 404
    get_missing = client.get("/sessions/custom-sess-100")
    assert get_missing.status_code == 404


# REQ-AGENT-035
def test_caller_auth_rejected_when_unauthenticated():
    """Verify unauthenticated requests are rejected with 401 when caller auth is enforced."""
    fake_agent = FakeAdkAgent()
    settings = Settings.from_env(
        env={
            "REQUIRE_REAL_DEPS": "1",
            "ALLOWED_CALLERS": "sa-cd-deployer@susmith-fde-ticket-router.iam.gserviceaccount.com",
        }
    )
    app = create_app(agent=fake_agent, settings=settings)
    client = TestClient(app)

    # Missing Authorization header -> 401
    resp_no_auth = client.post("/query", json={"message": "hello"})
    assert resp_no_auth.status_code == 401
    assert "Missing Authorization header" in resp_no_auth.json()["detail"]

    # Invalid header format -> 401
    resp_bad_hdr = client.post(
        "/query",
        json={"message": "hello"},
        headers={"Authorization": "NotBearer xyz"},
    )
    assert resp_bad_hdr.status_code == 401
    assert "Invalid Authorization header format" in resp_bad_hdr.json()["detail"]

    # Unauthorized caller email -> 403
    with patch(
        "google.oauth2.id_token.verify_oauth2_token",
        return_value={"email": "unknown-caller@evil.com", "email_verified": True},
    ):
        resp_unauth_caller = client.post(
            "/query",
            json={"message": "hello"},
            headers={"Authorization": "Bearer signed.google.jwt"},
        )
        assert resp_unauth_caller.status_code == 403
        assert "not in ALLOWED_CALLERS" in resp_unauth_caller.json()["detail"]

    # Magic test-token prefix MUST NOT bypass Google OIDC verification in production -> 401
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Invalid signature")):
        resp_forged_prefix = client.post(
            "/query",
            json={"message": "hello"},
            headers={
                "Authorization": "Bearer test-token-sa-cd-deployer@susmith-fde-ticket-router.iam.gserviceaccount.com"
            },
        )
        assert resp_forged_prefix.status_code == 401

    # Invalid Google ID token exception -> 401
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Token expired")):
        resp_invalid_jwt = client.post(
            "/query",
            json={"message": "hello"},
            headers={"Authorization": "Bearer real.looking.jwt"},
        )
        assert resp_invalid_jwt.status_code == 401
        assert "Invalid Google ID token" in resp_invalid_jwt.json()["detail"]


# REQ-AGENT-035
def test_fallback_app_delegates_to_root_agent_by_default():
    """Verify default create_app() wires root_agent correctly."""
    from ticket_router.agents.triage_router.adk_agent import root_agent

    app = create_app()
    assert app.state.agent is root_agent


# REQ-AGENT-035
def test_caller_auth_accepted_for_allowed_caller():
    """Verify requests with valid caller token in ALLOWED_CALLERS are accepted."""
    fake_agent = FakeAdkAgent()
    settings = Settings.from_env(
        env={
            "REQUIRE_REAL_DEPS": "1",
            "ALLOWED_CALLERS": "sa-cd-deployer@susmith-fde-ticket-router.iam.gserviceaccount.com",
        }
    )
    app = create_app(
        agent=fake_agent,
        settings=settings,
        token_verifier=lambda token, aud: {
            "email": "sa-cd-deployer@susmith-fde-ticket-router.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    client = TestClient(app)

    resp = client.post(
        "/query",
        json={"message": "hello from deployer"},
        headers={"Authorization": "Bearer signed.deployer.jwt"},
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Echo: hello from deployer"


# REQ-AGENT-035
def test_env_contract_shared_and_consistent():
    """Verify env_contract defines canonical variables and filters reserved names."""
    contract = get_triage_env_contract(project="test-proj", region="us-central1")
    assert contract["GOOGLE_CLOUD_PROJECT"] == "test-proj"
    assert contract["GOOGLE_CLOUD_LOCATION"] == "us-central1"
    assert contract["SERVICE"] == "triage_fallback"
    assert contract["ROUTER_S1_ENDPOINT"]
    assert contract["TICKET_DB_MCP_URL"]

    # Reserved names are filtered for Agent Engine control plane
    filtered = filter_reserved_env_vars(contract)
    assert "GOOGLE_CLOUD_PROJECT" not in filtered
    assert "GOOGLE_CLOUD_LOCATION" not in filtered
    assert "TICKET_DB_MCP_URL" in filtered
    assert "SERVICE" in filtered
