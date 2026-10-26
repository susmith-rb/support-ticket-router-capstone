import pytest


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch):
    """Enforce offline test invariant by failing on un-mocked network requests."""
    def _fail_network(*args, **kwargs):
        raise RuntimeError("Unexpected network call in offline test. Mock urllib.request.urlopen.")

    monkeypatch.setattr("urllib.request.urlopen", _fail_network)
