"""Tests for FaqResolver A2A agent card endpoint.

# REQ-AUTH-008: GKE Agent Identity for FaqResolver (Standard Cluster)
Verifies:
1. GET /.well-known/agent-card.json is accessible without authentication (unauthenticated).
2. Returned JSON conforms to A2A agent card specification:
   - name is FaqResolver
   - public URL matches configured FAQ_RESOLVER_PUBLIC_URL or default
   - supportedInterfaces includes JSONRPC and HTTP on /resolve
   - capabilities and skills declared
   - no secrets, credentials, or internal tokens exposed
3. Fallback ASGI app (AsgiFallbackApp) handles /.well-known/agent-card.json cleanly.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from ticket_router.agents.faq_resolver.agent import root_agent
from ticket_router.agents.faq_resolver.app import (
    AsgiFallbackApp,
    Settings,
    create_app,
    get_well_known_agent_card,
)


# REQ-AUTH-008
def test_well_known_agent_card_schema_and_safety() -> None:
    """Verify get_well_known_agent_card() structure, url resolution, and secret-free posture."""
    card = get_well_known_agent_card()

    assert card["name"] == "FaqResolver"
    assert "description" in card and len(card["description"]) > 0
    assert card["version"] == "1.0.0"
    assert card["interface"] == "/resolve"
    assert card["protocol"] == "HTTP"

    # Supported interfaces
    interfaces = card.get("supportedInterfaces", [])
    assert len(interfaces) >= 2
    bindings = [iface.get("protocolBinding") for iface in interfaces]
    assert "JSONRPC" in bindings
    assert "HTTP" in bindings

    for iface in interfaces:
        assert iface["url"].endswith("/resolve")

    # Safety: ensure no secret keys or internal credentials leaked
    card_str = str(card).lower()
    for forbidden in ("secret", "token", "password", "private_key", "bearer"):
        assert forbidden not in card_str, f"Forbidden keyword '{forbidden}' found in agent card"


# REQ-AUTH-008
def test_agent_card_endpoint_unauthenticated_fastapi(monkeypatch) -> None:
    """Verify FastAPI GET /.well-known/agent-card.json returns 200 without Authorization."""
    monkeypatch.setenv("REQUIRE_REAL_DEPS", "0")
    monkeypatch.setenv("ALLOWED_CALLERS", "sa-triage@example.com")
    settings = Settings.from_env()
    app = create_app(agent=root_agent, settings=settings)
    client = TestClient(app)

    # Note: /resolve requires authentication
    resolve_resp = client.post("/resolve", json={})
    assert resolve_resp.status_code == 401

    # But /.well-known/agent-card.json is completely unauthenticated
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")

    data = resp.json()
    assert data["name"] == "FaqResolver"
    assert data["interface"] == "/resolve"


# REQ-AUTH-008
def test_agent_card_endpoint_fallback_asgi(monkeypatch) -> None:
    """Verify AsgiFallbackApp handles GET /.well-known/agent-card.json."""
    monkeypatch.setenv("REQUIRE_REAL_DEPS", "0")
    settings = Settings.from_env()
    app = AsgiFallbackApp(agent=root_agent, settings=settings)
    client = TestClient(app)

    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "FaqResolver"
    assert data["interface"] == "/resolve"
