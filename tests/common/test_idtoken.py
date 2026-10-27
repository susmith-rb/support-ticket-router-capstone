"""Hermetic tests for Google OIDC ID token fetching and per-audience caching.

# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

import base64
import json
from unittest.mock import MagicMock
import pytest

from ticket_router.common.idtoken import (
    clear_id_token_cache,
    get_id_token,
)


def make_fake_jwt(exp: float, email: str = "test@example.com") -> str:
    """Construct mock signed JWT token with exp payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode("utf-8")).decode("utf-8")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "email": email, "email_verified": True}).encode("utf-8")
    ).decode("utf-8")
    return f"{header}.{payload}.fake_signature"


class MockClock:
    """Mock monotonic clock for testing expiration."""

    def __init__(self, current_time: float = 1000000.0) -> None:
        self.current_time = current_time

    def get_time(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


@pytest.fixture(autouse=True)
def reset_cache():
    """Ensure clean cache before and after each test."""
    clear_id_token_cache()
    yield
    clear_id_token_cache()


def test_get_id_token_fetches_and_caches(monkeypatch):
    """Verify get_id_token calls fetch_id_token and caches by audience."""
    clock = MockClock(current_time=1000.0)
    fake_token = make_fake_jwt(exp=1000.0 + 3600.0)  # expires in 1 hour

    mock_fetch = MagicMock(return_value=fake_token)
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", mock_fetch)

    # First call: fetches token
    tok1 = get_id_token("https://target-service.run.app", clock=clock.get_time)
    assert tok1 == fake_token
    assert mock_fetch.call_count == 1

    # Second call for same audience within TTL: returned from cache
    clock.advance(100.0)
    tok2 = get_id_token("https://target-service.run.app", clock=clock.get_time)
    assert tok2 == fake_token
    assert mock_fetch.call_count == 1  # No additional network fetch

    # Call for different audience: fetches new token
    fake_token2 = make_fake_jwt(exp=1000.0 + 3600.0, email="diff@example.com")
    mock_fetch.return_value = fake_token2
    tok3 = get_id_token("https://another-service.run.app", clock=clock.get_time)
    assert tok3 == fake_token2
    assert mock_fetch.call_count == 2


def test_get_id_token_cache_expires(monkeypatch):
    """Verify token is re-fetched once expired (past exp - 60s)."""
    clock = MockClock(current_time=1000.0)
    token_1 = make_fake_jwt(exp=1000.0 + 120.0)  # expires in 120s, cache TTL is 60s
    token_2 = make_fake_jwt(exp=1000.0 + 1000.0)

    mock_fetch = MagicMock(side_effect=[token_1, token_2])
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", mock_fetch)

    # First call: cache valid until 1000 + 60 = 1060s
    tok = get_id_token("https://expiring-service.run.app", clock=clock.get_time)
    assert tok == token_1
    assert mock_fetch.call_count == 1

    # Advance 50s (at 1050s, still < 1060s): hits cache
    clock.advance(50.0)
    tok_cached = get_id_token("https://expiring-service.run.app", clock=clock.get_time)
    assert tok_cached == token_1
    assert mock_fetch.call_count == 1

    # Advance 20s (at 1070s > 1060s): expired, refetches
    clock.advance(20.0)
    tok_refreshed = get_id_token("https://expiring-service.run.app", clock=clock.get_time)
    assert tok_refreshed == token_2
    assert mock_fetch.call_count == 2


def test_get_id_token_force_refresh(monkeypatch):
    """Verify force_refresh=True bypasses cache."""
    clock = MockClock(current_time=1000.0)
    token_1 = make_fake_jwt(exp=5000.0)
    token_2 = make_fake_jwt(exp=6000.0)

    mock_fetch = MagicMock(side_effect=[token_1, token_2])
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", mock_fetch)

    tok1 = get_id_token("https://refresh-service.run.app", clock=clock.get_time)
    assert tok1 == token_1
    assert mock_fetch.call_count == 1

    tok2 = get_id_token("https://refresh-service.run.app", clock=clock.get_time, force_refresh=True)
    assert tok2 == token_2
    assert mock_fetch.call_count == 2
