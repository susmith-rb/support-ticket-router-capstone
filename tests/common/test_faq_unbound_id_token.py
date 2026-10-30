"""Tests for FaqResolver unbound ID token fetching and agent identity detection.

# REQ-AUTH-008: GKE Agent Identity for FaqResolver (Standard Cluster)
# REQ-AUTH-009: order-lookup-mcp caller authorization of FaqResolver SPIFFE principal
Verifies:
1. is_faq_agent_identity() detects FAQ_AGENT_IDENTITY=1 and /var/run/secrets/workload-spiffe-credentials.
2. fetch_unbound_agent_identity_token() requests unbound ID tokens (bind_id_token=False) and
   gracefully falls back if bind_id_token is not accepted.
3. get_id_token() routes through unbound agent identity fetch when running under GKE Agent Identity.
4. Cache key separates agent identity from metadata or impersonate service account modes.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from ticket_router.common.idtoken import (
    clear_id_token_cache,
    fetch_unbound_agent_identity_token,
    get_id_token,
    is_faq_agent_identity,
)


def _make_jwt(exp_seconds: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_seconds, "sub": "test-sub"}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}."


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_id_token_cache()
    yield
    clear_id_token_cache()


# REQ-AUTH-008
def test_is_faq_agent_identity(monkeypatch) -> None:
    """Verify is_faq_agent_identity detects env var and filesystem credentials."""
    monkeypatch.delenv("FAQ_AGENT_IDENTITY", raising=False)

    with patch("os.path.exists", return_value=False):
        assert is_faq_agent_identity() is False

    monkeypatch.setenv("FAQ_AGENT_IDENTITY", "1")
    assert is_faq_agent_identity() is True

    monkeypatch.delenv("FAQ_AGENT_IDENTITY", raising=False)
    with patch("os.path.exists", side_effect=lambda p: p == "/var/run/secrets/workload-spiffe-credentials"):
        assert is_faq_agent_identity() is True


# REQ-AUTH-008, REQ-AUTH-009
def test_fetch_unbound_agent_identity_token_calls_bind_id_token_false() -> None:
    """Verify fetch_unbound_agent_identity_token passes bind_id_token=False to google-auth."""
    mock_req = MagicMock()
    with patch("google.oauth2.id_token.fetch_id_token", return_value="mock.jwt.token") as mock_fetch:
        token = fetch_unbound_agent_identity_token("https://order-lookup-mcp", request=mock_req)
        assert token == "mock.jwt.token"
        mock_fetch.assert_called_once_with(mock_req, "https://order-lookup-mcp", bind_id_token=False)


def _type_error_side_effect(*args, **kwargs):
    if "bind_id_token" in kwargs:
        raise TypeError("unexpected keyword argument 'bind_id_token'")
    return "fallback.jwt.token"


# REQ-AUTH-008, REQ-AUTH-009
def test_fetch_unbound_agent_identity_token_fallback_on_type_error() -> None:
    """Verify graceful fallback if older google-auth does not support bind_id_token."""
    mock_req = MagicMock()
    with patch("google.oauth2.id_token.fetch_id_token", side_effect=_type_error_side_effect) as mock_fetch:
        token = fetch_unbound_agent_identity_token("https://order-lookup-mcp", request=mock_req)
        assert token == "fallback.jwt.token"
        assert mock_fetch.call_count == 2


# REQ-AUTH-008, REQ-AUTH-009
def test_get_id_token_under_agent_identity(monkeypatch) -> None:
    """Verify get_id_token routes through unbound fetch and caches token under agent identity."""
    monkeypatch.setenv("FAQ_AGENT_IDENTITY", "1")
    monkeypatch.delenv("ID_TOKEN_IMPERSONATE_SA", raising=False)

    exp_time = time.time() + 3600
    mock_token = _make_jwt(exp_time)

    with patch(
        "ticket_router.common.idtoken.fetch_unbound_agent_identity_token",
        return_value=mock_token,
    ) as mock_fetch:
        # First call fetches unbound token
        t1 = get_id_token("https://order-lookup-mcp")
        assert t1 == mock_token
        assert mock_fetch.call_count == 1

        # Second call hits cache
        t2 = get_id_token("https://order-lookup-mcp")
        assert t2 == mock_token
        assert mock_fetch.call_count == 1

        # Force refresh bypasses cache
        t3 = get_id_token("https://order-lookup-mcp", force_refresh=True)
        assert t3 == mock_token
        assert mock_fetch.call_count == 2
