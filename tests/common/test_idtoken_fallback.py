"""ID token fallback when the metadata server refuses the `default` SA alias (Cloud Build)."""

# REQ-AGENT-006

import pytest

from ticket_router.common import idtoken


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_falls_back_to_explicit_sa_email(monkeypatch):
    # REQ-AGENT-006
    import requests

    def refused(req, audience):
        raise RuntimeError("Status: 404 Response: b'please provide a user-specified service account\\n'")

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/default/email"):
            return _Resp("sa-cd-deployer@p.iam.gserviceaccount.com\n")
        return _Resp("tok.en.jwt")

    monkeypatch.setattr(requests, "get", fake_get)
    tok = idtoken._fetch_id_token_with_fallback(object(), "https://svc", refused)
    assert tok == "tok.en.jwt"
    assert calls[1].endswith("/sa-cd-deployer@p.iam.gserviceaccount.com/identity")


def test_other_errors_propagate():
    # REQ-AGENT-006
    def boom(req, audience):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        idtoken._fetch_id_token_with_fallback(object(), "https://svc", boom)
