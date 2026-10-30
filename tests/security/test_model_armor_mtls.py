"""Hermetic tests for Model Armor mTLS endpoint rewriting and credential management.

# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

import concurrent.futures
from unittest.mock import MagicMock

import pytest
import requests

from ticket_router.security.model_armor import ModelArmorClient


def _make_dummy_response() -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = b'{"sanitizationResult": {"filterMatchState": "NO_MATCH_FOUND", "filterResults": {}}}'
    return resp


def test_model_armor_mtls_on_endpoint_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is on and no session injected, request goes to .rep.mtls and AuthorizedSession handles auth.

    # REQ-AUTH-006
    """
    mock_session = MagicMock(spec=requests.Session)
    mock_session.post.return_value = _make_dummy_response()

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, True),
    )

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
    )
    # Session is lazily None before first use
    assert client._session is None

    verdict = client.sanitize_user_prompt("Hello world")
    assert verdict.is_clean is True

    assert mock_session.post.call_count == 1
    call_args, call_kwargs = mock_session.post.call_args
    url = call_args[0]
    assert url == "https://modelarmor.us-central1.rep.mtls.googleapis.com/v1/projects/p/locations/us-central1/templates/t:sanitizeUserPrompt"
    # No manual Bearer header in default path: AuthorizedSession handles credentials
    assert "Authorization" not in call_kwargs.get("headers", {})
    assert call_kwargs.get("headers", {}).get("Content-Type") == "application/json"


def test_model_armor_mtls_off_endpoint_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is off and no session injected, request goes to plain .rep endpoint.

    # REQ-AUTH-006
    """
    mock_session = MagicMock(spec=requests.Session)
    mock_session.post.return_value = _make_dummy_response()

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, False),
    )

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
    )
    verdict = client.sanitize_user_prompt("Hello world")
    assert verdict.is_clean is True

    call_args, call_kwargs = mock_session.post.call_args
    url = call_args[0]
    assert url == "https://modelarmor.us-central1.rep.googleapis.com/v1/projects/p/locations/us-central1/templates/t:sanitizeUserPrompt"
    assert "Authorization" not in call_kwargs.get("headers", {})


def test_model_armor_injected_session_preserves_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a session or token provider is injected, manual Bearer header is preserved.

    # REQ-AUTH-006
    """
    injected_session = MagicMock(spec=requests.Session)
    injected_session.is_mtls = False
    injected_session.post.return_value = _make_dummy_response()

    token_called = []

    def fake_token_provider() -> str:
        token_called.append(True)
        return "injected-bearer-token"

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
        session=injected_session,
        token_provider=fake_token_provider,
    )
    verdict = client.sanitize_user_prompt("Test prompt")
    assert verdict.is_clean is True

    assert len(token_called) == 1
    call_args, call_kwargs = injected_session.post.call_args
    assert call_args[0] == "https://modelarmor.us-central1.rep.googleapis.com/v1/projects/p/locations/us-central1/templates/t:sanitizeUserPrompt"
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer injected-bearer-token"


def test_model_armor_injected_session_with_mtls() -> None:
    """When an injected session has is_mtls=True, URL rewrites to .mtls.

    # REQ-AUTH-006
    """
    injected_session = MagicMock(spec=requests.Session)
    injected_session.is_mtls = True
    injected_session.post.return_value = _make_dummy_response()

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
        session=injected_session,
        token_provider=lambda: "token",
    )
    client.sanitize_user_prompt("Test prompt")

    call_args, _ = injected_session.post.call_args
    assert call_args[0] == "https://modelarmor.us-central1.rep.mtls.googleapis.com/v1/projects/p/locations/us-central1/templates/t:sanitizeUserPrompt"


def test_model_armor_thread_safe_lazy_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent threads accessing client.session obtain the same session instance safely.

    # REQ-AUTH-006
    """
    call_count = 0

    def mock_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        s = MagicMock(spec=requests.Session)
        s.is_mtls = True
        return s, True

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_create,
    )

    client = ModelArmorClient(template="projects/p/locations/us-central1/templates/t")

    def get_sess():
        return client.session

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(get_sess) for _ in range(16)]
        results = [f.result() for f in futures]

    assert call_count == 1
    assert len({id(r) for r in results}) == 1


def test_model_armor_create_session_failure_degrades_and_counts_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When create_google_session raises, sanitize_user_prompt degrades, never caches session, and counts towards breaker.

    # REQ-AUTH-006: F1 fix
    """
    import google.auth.exceptions as auth_exceptions

    call_count = 0

    def mock_boom(**kwargs):
        nonlocal call_count
        call_count += 1
        raise auth_exceptions.DefaultCredentialsError("ADC unavailable")

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_boom,
    )

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
    )

    # First call: should degrade gracefully and increment breaker failure count
    verdict1 = client.sanitize_user_prompt("test input 1")
    assert verdict1.action == "DEGRADE"
    assert verdict1.degraded is True
    assert "DefaultCredentialsError" in (verdict1.reason or "")
    assert client._session is None  # Never caches failed session
    assert client.circuit_breaker.consecutive_failures == 1

    # Second call: still degrades and increments failure count
    verdict2 = client.sanitize_user_prompt("test input 2")
    assert verdict2.action == "DEGRADE"
    assert verdict2.degraded is True
    assert client._session is None
    assert client.circuit_breaker.consecutive_failures == 2

    # Third call: reaches failure_threshold (3), breaker opens
    verdict3 = client.sanitize_model_response("model output")
    assert verdict3.action == "DEGRADE"
    assert verdict3.degraded is True
    assert client.circuit_breaker.state == "OPEN"

    # Fourth call: circuit breaker is open, fast-fail without calling create_google_session
    verdict4 = client.sanitize_user_prompt("test input 4")
    assert verdict4.action == "DEGRADE"
    assert verdict4.degraded is True
    assert "CircuitBreaker" in (verdict4.reason or "")
    assert call_count == 3  # Not called again when open


def test_model_armor_uses_max_refresh_attempts_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model Armor requests create_google_session with max_refresh_attempts=1 for bounded latency.

    # REQ-AUTH-006: F2 fix
    """
    captured_kwargs = {}

    def mock_create(**kwargs):
        captured_kwargs.update(kwargs)
        s = MagicMock(spec=requests.Session)
        s.is_mtls = True
        return s, True

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_create,
    )

    client = ModelArmorClient(
        template="projects/p/locations/us-central1/templates/t",
    )
    _ = client.session
    assert captured_kwargs.get("max_refresh_attempts") == 1

