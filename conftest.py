import os
import pathlib
import sys
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent
RAG_DIR = REPO_ROOT / "rag"
PIPELINE_DIR = REPO_ROOT / "training" / "router_s1" / "pipeline"

for p in (str(REPO_ROOT), str(RAG_DIR), str(PIPELINE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def block_unmocked_network(monkeypatch):
    """Enforce offline test invariant by failing on un-mocked network requests."""
    def _fail_network(*args, **kwargs):
        raise RuntimeError("Unexpected network call in offline test. Mock urllib.request.urlopen.")

    monkeypatch.setattr("urllib.request.urlopen", _fail_network)
