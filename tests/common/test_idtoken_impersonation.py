"""Hermetic tests for SA impersonation ID token minting via IAM Credentials API.

# REQ-AUTH-004: SPIFFE Agent Identity for TriageRouter (IAM Credentials generateIdToken)
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import requests

from ticket_router.common import auth
from ticket_router.common.idtoken import clear_id_token_cache, get_id_token


def _make_fake_jwt(exp: float | None, sub_sa: str = "sa-triage@proj.iam.gserviceaccount.com") -> str:
    """Build a synthetic JWT string with optional exp claim (no real keys or PII)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8")).decode("utf-8")
    payload_dict: dict[str, Any] = {"email": sub_sa, "email_verified": True}
    if exp is not None:
        payload_dict["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode("utf-8")).decode("utf-8")
    return f"{header}.{payload}.synthetic_sig"


class _FakeClock:
    def __init__(self, now: float = 2_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, delta_s: float) -> None:
        self.now += delta_s


class _FakeHttpResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}: {self._body}", response=self)

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeHttpLayer:
    """Hermetic fake HTTP layer recording POST calls and forbidding metadata GETs."""

    def __init__(self, responses: list[_FakeHttpResponse]) -> None:
        self._responses = list(responses)
        self.posts: list[dict[str, Any]] = []

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"Metadata HTTP GET must not be called when impersonating SA: {args}")

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeHttpResponse:
        self.posts.append(
            {
                "url": url,
                "json": json,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError("Unexpected extra HTTP POST to iamcredentials")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _clean_caches():
    clear_id_token_cache()
    auth.reset_auth_cache()
    yield
    clear_id_token_cache()
    auth.reset_auth_cache()


# REQ-AUTH-004
def test_impersonate_sa_mints_via_iamcredentials_and_skips_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ID_TOKEN_IMPERSONATE_SA is set, get_id_token POSTs to iamcredentials and skips metadata."""
    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    audience = "https://ticket-db-mcp.a.run.app"
    clock = _FakeClock(10_000.0)
    minted_jwt = _make_fake_jwt(exp=10_000.0 + 3600.0, sub_sa=sa_email)

    monkeypatch.setenv("ID_TOKEN_IMPERSONATE_SA", sa_email)
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "adc-agent-access-token")

    def _forbid_metadata_fetch(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("google.oauth2.id_token.fetch_id_token must be skipped when ID_TOKEN_IMPERSONATE_SA is set")

    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", _forbid_metadata_fetch)
    fake_http = _FakeHttpLayer([_FakeHttpResponse(200, {"token": minted_jwt})])
    monkeypatch.setattr(requests, "get", fake_http.get)
    monkeypatch.setattr(requests, "post", fake_http.post)
    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda: (fake_http, False),
    )

    tok = get_id_token(audience, clock=clock)
    assert tok == minted_jwt
    assert len(fake_http.posts) == 1
    call = fake_http.posts[0]
    assert call["url"] == (
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateIdToken"
    )
    assert call["json"] == {"audience": audience, "includeEmail": True}
    assert call["headers"]["Authorization"] == "Bearer adc-agent-access-token"
    assert call["timeout"] == 10


# REQ-AUTH-004
def test_impersonate_sa_caching_ttl_and_force_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Impersonated ID tokens preserve per-audience (exp - 60s) caching and force_refresh."""
    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    aud_a = "https://ticket-db-mcp.a.run.app"
    aud_b = "https://faq.internal.example"
    clock = _FakeClock(50_000.0)

    tok_a1 = _make_fake_jwt(exp=50_000.0 + 180.0, sub_sa=sa_email)  # valid until 50120.0
    tok_b1 = _make_fake_jwt(exp=50_000.0 + 3600.0, sub_sa=sa_email)
    tok_a2 = _make_fake_jwt(exp=50_000.0 + 3600.0, sub_sa=sa_email)
    tok_a3 = _make_fake_jwt(exp=50_000.0 + 7200.0, sub_sa=sa_email)

    monkeypatch.setenv("ID_TOKEN_IMPERSONATE_SA", f"  {sa_email}  ")
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "adc-token-1")
    fake_http = _FakeHttpLayer(
        [
            _FakeHttpResponse(200, {"token": tok_a1}),
            _FakeHttpResponse(200, {"token": tok_b1}),
            _FakeHttpResponse(200, {"token": tok_a2}),
            _FakeHttpResponse(200, {"token": tok_a3}),
        ]
    )

    # Initial mint for aud_a via injected fake HTTP session
    assert get_id_token(aud_a, clock=clock, session=fake_http) == tok_a1
    assert len(fake_http.posts) == 1
    assert fake_http.posts[0]["url"].endswith(f"/serviceAccounts/{sa_email}:generateIdToken")

    # Within TTL (50_000 + 119 < 50_120): served from cache without HTTP POST
    clock.advance(119.0)
    assert get_id_token(aud_a, clock=clock, session=fake_http) == tok_a1
    assert len(fake_http.posts) == 1

    # Different audience aud_b: mints separately
    assert get_id_token(aud_b, clock=clock, session=fake_http) == tok_b1
    assert len(fake_http.posts) == 2
    assert fake_http.posts[1]["json"] == {"audience": aud_b, "includeEmail": True}

    # Past (exp - 60s) boundary (50_121 > 50_120): re-mints for aud_a
    clock.advance(2.0)
    assert get_id_token(aud_a, clock=clock, session=fake_http) == tok_a2
    assert len(fake_http.posts) == 3

    # force_refresh=True bypasses valid cache
    assert get_id_token(aud_a, clock=clock, force_refresh=True, session=fake_http) == tok_a3
    assert len(fake_http.posts) == 4


# REQ-AUTH-004
def test_impersonate_sa_empty_env_uses_metadata_and_isolates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty/whitespace ID_TOKEN_IMPERSONATE_SA uses metadata fetch and does not share cache with SA mode."""
    audience = "https://human-queue.a.run.app"
    clock = _FakeClock(1_000.0)
    meta_tok = _make_fake_jwt(exp=5_000.0, sub_sa="default-sa@test-proj.iam.gserviceaccount.com")
    imp_tok = _make_fake_jwt(exp=5_000.0, sub_sa="sa-triage@test-proj.iam.gserviceaccount.com")

    monkeypatch.setenv("ID_TOKEN_IMPERSONATE_SA", "   ")
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", lambda req, aud: meta_tok)
    assert get_id_token(audience, request=object(), clock=clock) == meta_tok

    # Switching ID_TOKEN_IMPERSONATE_SA on must NOT return the cached non-impersonated token
    monkeypatch.setenv("ID_TOKEN_IMPERSONATE_SA", "sa-triage@test-proj.iam.gserviceaccount.com")
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "adc-token")
    fake_http = _FakeHttpLayer([_FakeHttpResponse(200, {"token": imp_tok})])
    assert get_id_token(audience, clock=clock, session=fake_http) == imp_tok
    assert len(fake_http.posts) == 1


# REQ-AUTH-004
def test_impersonate_sa_http_error_propagates_and_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """IAM Credentials 403/500 errors propagate and do not poison the ID token cache."""
    sa_email = "sa-triage@test-proj.iam.gserviceaccount.com"
    audience = "https://ticket-db-mcp.a.run.app"
    clock = _FakeClock(3_000.0)
    good_tok = _make_fake_jwt(exp=None, sub_sa=sa_email)  # also exercises 3000s default fallback when exp absent

    monkeypatch.setenv("ID_TOKEN_IMPERSONATE_SA", sa_email)
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "adc-token")
    fake_http = _FakeHttpLayer(
        [
            _FakeHttpResponse(403, {"error": {"message": "Permission iam.serviceAccounts.getOpenIdToken denied"}}),
            _FakeHttpResponse(200, {"token": good_tok}),
        ]
    )

    with pytest.raises(requests.HTTPError, match="403"):
        get_id_token(audience, clock=clock, session=fake_http)

    # Subsequent retry succeeds and caches with 3000s fallback
    assert get_id_token(audience, clock=clock, session=fake_http) == good_tok
    clock.advance(2999.0)
    assert get_id_token(audience, clock=clock, session=fake_http) == good_tok
    assert len(fake_http.posts) == 2
