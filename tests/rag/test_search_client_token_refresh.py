"""Regression: SearchClient must not cache the ADC access token string.

# REQ-AGENT-011

Live 2026-10-31: the cached token expired ~60 min after the FaqResolver pod started, every
Agent Search call failed, and order questions escalated with reason=search_failure.
"""

from ticket_router.retrieval import search_client as sc


# REQ-AGENT-011
def test_get_token_asks_credential_cache_every_call(monkeypatch) -> None:
    tokens = iter(["tok-1", "tok-2"])
    monkeypatch.setattr(sc, "get_access_token", lambda: next(tokens))
    client = sc.SearchClient()
    assert client.get_token() == "tok-1"
    assert client.get_token() == "tok-2"  # refreshed token is picked up


# REQ-AGENT-011
def test_injected_token_is_used_as_is(monkeypatch) -> None:
    monkeypatch.setattr(sc, "get_access_token", lambda: "adc")
    assert sc.SearchClient(token="static").get_token() == "static"
