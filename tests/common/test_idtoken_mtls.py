"""Hermetic tests for IAM Credentials mTLS endpoint rewriting and unbound ID token fetching.

# REQ-AUTH-006: mTLS-Aware Google API Transports (certificate-bound agent identity tokens)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.auth.transport.requests import AuthorizedSession

from ticket_router.common import auth, idtoken
from ticket_router.common.idtoken import (
    _fetch_id_token_with_fallback,
    _generate_id_token_via_iamcredentials,
)


@pytest.fixture(autouse=True)
def reset_iam_session_before_each_test():
    idtoken.clear_id_token_cache()
    yield
    idtoken.clear_id_token_cache()


def _make_dummy_response(token: str = "mock-id-token") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"token": token}
    return resp


def test_iamcredentials_mtls_on_url_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is on and no session injected, IAM Credentials calls iamcredentials.mtls.googleapis.com.

    # REQ-AUTH-006
    """
    mock_session = MagicMock(spec=AuthorizedSession)
    mock_session.is_mtls = True
    mock_session.post.return_value = _make_dummy_response("jwt-mtls-on")

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, True),
    )

    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    token = _generate_id_token_via_iamcredentials(sa_email, "https://target-service")
    assert token == "jwt-mtls-on"

    assert mock_session.post.call_count == 1
    call_args, call_kwargs = mock_session.post.call_args
    url = call_args[0]
    assert url == (
        f"https://iamcredentials.mtls.googleapis.com/v1/projects/-/"
        f"serviceAccounts/{sa_email}:generateIdToken"
    )
    assert call_kwargs.get("json") == {"audience": "https://target-service", "includeEmail": True}
    # AuthorizedSession handles credentials automatically
    assert "Authorization" not in call_kwargs.get("headers", {})


def test_iamcredentials_mtls_off_url_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mTLS is off and no session injected, IAM Credentials calls iamcredentials.googleapis.com.

    # REQ-AUTH-006
    """
    mock_session = MagicMock(spec=AuthorizedSession)
    mock_session.is_mtls = False
    mock_session.post.return_value = _make_dummy_response("jwt-mtls-off")

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (mock_session, False),
    )

    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    token = _generate_id_token_via_iamcredentials(sa_email, "https://target-service")
    assert token == "jwt-mtls-off"

    call_args, _ = mock_session.post.call_args
    url = call_args[0]
    assert url == (
        f"https://iamcredentials.googleapis.com/v1/projects/-/"
        f"serviceAccounts/{sa_email}:generateIdToken"
    )


def test_iamcredentials_injected_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected session is used directly, preserving manual Bearer header and rewrites if is_mtls=True.

    # REQ-AUTH-006
    """
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "injected-bearer")

    # 1. Plain injected session
    fake_plain = MagicMock()
    fake_plain.is_mtls = False
    fake_plain.post.return_value = _make_dummy_response("token-plain")

    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    tok1 = _generate_id_token_via_iamcredentials(sa_email, "https://aud", session=fake_plain)
    assert tok1 == "token-plain"
    call1_url = fake_plain.post.call_args[0][0]
    assert call1_url.startswith("https://iamcredentials.googleapis.com")
    assert fake_plain.post.call_args[1]["headers"]["Authorization"] == "Bearer injected-bearer"

    # 2. mTLS injected session
    fake_mtls = MagicMock()
    fake_mtls.is_mtls = True
    fake_mtls.post.return_value = _make_dummy_response("token-mtls")

    tok2 = _generate_id_token_via_iamcredentials(sa_email, "https://aud", session=fake_mtls)
    assert tok2 == "token-mtls"
    call2_url = fake_mtls.post.call_args[0][0]
    assert call2_url.startswith("https://iamcredentials.mtls.googleapis.com")


def test_fetch_id_token_requests_unbound_token() -> None:
    """fetch_id_token must be called with bind_id_token=False for un-bound metadata ID tokens.

    # REQ-AUTH-006
    """
    captured_kwargs = {}

    def mock_fetch(req: object, audience: str, **kwargs: object) -> str:
        captured_kwargs.update(kwargs)
        return "unbound-id-token"

    tok = _fetch_id_token_with_fallback(object(), "https://svc", mock_fetch)
    assert tok == "unbound-id-token"
    assert captured_kwargs.get("bind_id_token") is False


def test_fetch_id_token_fallback_on_type_error() -> None:
    """When fetch does not accept bind_id_token (older google-auth), fallback gracefully without kwarg.

    # REQ-AUTH-006
    """
    call_records = []

    def mock_old_fetch(req: object, audience: str) -> str:
        call_records.append(audience)
        return "fallback-old-id-token"

    tok = _fetch_id_token_with_fallback(object(), "https://svc", mock_old_fetch)
    assert tok == "fallback-old-id-token"
    assert call_records == ["https://svc"]


def test_fetch_id_token_unrelated_type_error_propagates() -> None:
    """If fetch raises TypeError unrelated to bind_id_token, it must propagate.

    # REQ-AUTH-006
    """
    def broken_fetch(req: object, audience: str, **kwargs: object) -> str:
        raise TypeError("unrelated type error inside fetch body")

    with pytest.raises(TypeError, match="unrelated type error inside fetch body"):
        _fetch_id_token_with_fallback(object(), "https://svc", broken_fetch)


def test_iamcredentials_reuses_cached_session_and_resets_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_generate_id_token_via_iamcredentials reuses module-level session and resets on exception.

    # REQ-AUTH-006: Nit fix
    """
    create_calls = 0

    def mock_create(*args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        s = MagicMock(spec=AuthorizedSession)
        s.is_mtls = False
        s.post.return_value = _make_dummy_response(f"token-{create_calls}")
        return s, False

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_create,
    )

    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"

    # Call 1: creates session
    t1 = _generate_id_token_via_iamcredentials(sa_email, "https://aud1")
    assert t1 == "token-1"
    assert create_calls == 1

    # Call 2: reuses cached session
    t2 = _generate_id_token_via_iamcredentials(sa_email, "https://aud2")
    assert t2 == "token-1"  # Same mock session whose post returns token-1
    assert create_calls == 1

    # Call 3: session.post raises -> exception should reset cached session
    cached_s, _ = idtoken._get_iam_session()
    cached_s.post.side_effect = RuntimeError("network drop")
    with pytest.raises(RuntimeError, match="network drop"):
        _generate_id_token_via_iamcredentials(sa_email, "https://aud3")

    # Cached session is now None
    assert idtoken._IAM_CACHED_SESSION is None

    # Call 4: creates new session
    t4 = _generate_id_token_via_iamcredentials(sa_email, "https://aud4")
    assert t4 == "token-2"
    assert create_calls == 2


def test_get_iam_session_concurrent_reset_never_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent _get_iam_session() and _reset_iam_session() must never return (None, ...).

    # REQ-AUTH-006
    """
    import concurrent.futures

    def mock_create(*args, **kwargs):
        s = MagicMock(spec=AuthorizedSession)
        s.is_mtls = True
        return s, True

    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        mock_create,
    )

    def reader() -> None:
        for _ in range(200):
            sess, is_mtls = idtoken._get_iam_session()
            assert sess is not None
            assert is_mtls is True

    def resetter() -> None:
        for _ in range(200):
            idtoken._reset_iam_session()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(reader) for _ in range(6)] + [pool.submit(resetter) for _ in range(2)]
        for f in futs:
            f.result()

