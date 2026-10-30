"""ID token fallback when the metadata server refuses the `default` SA alias (Cloud Build)."""

# REQ-AGENT-006

from unittest.mock import MagicMock

import pytest

from ticket_router.common import idtoken


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_falls_back_to_iamcredentials_generate_id_token(monkeypatch):
    # REQ-AGENT-006
    import requests

    from ticket_router.common import auth

    idtoken.clear_id_token_cache()

    def refused(req, audience):
        raise RuntimeError("Status: 404 Response: b'please provide a user-specified service account\\n'")

    posted = {}

    class _Json:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"token": "tok.en.jwt"}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.update(url=url, body=json, auth=(headers or {}).get("Authorization"))
        return _Json()

    fake_session = MagicMock()
    fake_session.post = fake_post
    monkeypatch.setattr(
        "ticket_router.common.google_http.create_google_session",
        lambda *args, **kwargs: (fake_session, False),
    )
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("sa-cd-deployer@p.iam.gserviceaccount.com\n"))
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(auth, "get_access_token", lambda scopes=None: "access-tok")
    tok = idtoken._fetch_id_token_with_fallback(object(), "https://svc", refused)
    assert tok == "tok.en.jwt"
    assert posted["url"].endswith("/serviceAccounts/sa-cd-deployer@p.iam.gserviceaccount.com:generateIdToken")
    assert posted["body"] == {"audience": "https://svc", "includeEmail": True}
    assert posted["auth"] == "Bearer access-tok"


def test_other_errors_propagate():
    # REQ-AGENT-006
    def boom(req, audience):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        idtoken._fetch_id_token_with_fallback(object(), "https://svc", boom)
