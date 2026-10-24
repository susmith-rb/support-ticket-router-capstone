"""Offline unit tests for rag/search_client.py.

Tests cover:
- build_filter construction for status, intent, and effective-window
- SearchClient execution, filter propagation, and latency tracking (REQ-RAG-RETR-009)
- Intent family filtering and automatic unconstrained fallback (REQ-RAG-RETR-010)
"""

from datetime import datetime, timezone
import json
import pytest
from search_client import (
    build_filter,
    clean_query,
    SearchClient,
    SearchResult,
    DEFAULT_ALLOWED_STATUSES,
)


# REQ-RAG-RETR-009
def test_build_filter_status():
    assert build_filter() == ""
    assert build_filter(allowed_statuses=["active"]) == 'status: ANY("active")'
    assert build_filter(allowed_statuses=["active", "draft"]) == 'status: ANY("active", "draft")'
    assert build_filter(allowed_statuses="active") == 'status: ANY("active")'


# REQ-RAG-RETR-010
def test_build_filter_intent():
    assert build_filter(intent_family="order_status") == 'intent_family: ANY("order_status")'
    assert build_filter(intent_family="account_payment") == 'intent_family: ANY("account_payment")'


# REQ-RAG-RETR-009
def test_build_filter_combined_status_intent_and_datetime():
    dt = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    expr = build_filter(
        allowed_statuses=["active", "draft"],
        intent_family="shipping_policy",
        effective_at=dt,
    )
    assert 'status: ANY("active", "draft")' in expr
    assert 'intent_family: ANY("shipping_policy")' in expr
    assert 'effective_from <= "2026-10-24T12:00:00Z"' in expr
    assert expr == 'status: ANY("active", "draft") AND intent_family: ANY("shipping_policy") AND effective_from <= "2026-10-24T12:00:00Z"'


# REQ-RAG-RETR-009
def test_build_filter_custom():
    expr = build_filter(allowed_statuses=["active"], custom_filter='theme: "late_delivery"')
    assert expr == 'status: ANY("active") AND (theme: "late_delivery")'


# REQ-RAG-RETR-001
def test_clean_query():
    assert clean_query("@AmazonHelp where is my [ORDER] #urgent") == "where is my urgent"
    assert clean_query("plain query") == "plain query"


class MockHTTPResponse:
    def __init__(self, data: dict):
        self.data = json.dumps(data).encode("utf-8")

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# REQ-RAG-RETR-009
def test_search_client_status_filter_applied(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({
            "results": [{"document": {"id": "faq-doc-1"}}],
            "totalSize": 1,
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token", default_allowed_statuses=["active", "draft"])
    res = client.search("where is my order", allowed_statuses=["active", "draft"])

    assert len(recorded_requests) == 1
    assert recorded_requests[0]["filter"] == 'status: ANY("active", "draft")'
    assert res.document_ids == ["faq-doc-1"]
    assert res.fallback_triggered is False
    assert res.latency_ms >= 0


# REQ-RAG-RETR-010
def test_search_client_intent_filter_no_fallback_when_sufficient(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({
            "results": [
                {"document": {"id": "faq-order-1"}},
                {"document": {"id": "faq-order-2"}},
            ],
            "totalSize": 2,
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    res = client.search(
        "tracking late order",
        intent_family="order_status",
        allowed_statuses=["active", "draft"],
        fallback_min_results=2,
    )

    assert len(recorded_requests) == 1
    assert recorded_requests[0]["filter"] == 'status: ANY("active", "draft") AND intent_family: ANY("order_status")'
    assert res.document_ids == ["faq-order-1", "faq-order-2"]
    assert res.fallback_triggered is False


# REQ-RAG-RETR-010
def test_search_client_intent_fallback_triggers_when_insufficient(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        # First request (intent filtered) returns only 1 doc (< 2 fallback_min_results)
        if "intent_family" in body.get("filter", ""):
            return MockHTTPResponse({
                "results": [{"document": {"id": "faq-order-1"}}],
                "totalSize": 1,
            })
        # Second request (fallback unfiltered for intent) returns 3 docs
        return MockHTTPResponse({
            "results": [
                {"document": {"id": "faq-fallback-1"}},
                {"document": {"id": "faq-fallback-2"}},
                {"document": {"id": "faq-fallback-3"}},
            ],
            "totalSize": 3,
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    res = client.search(
        "rare query",
        intent_family="shipping_policy",
        allowed_statuses=["active", "draft"],
        fallback_min_results=2,
    )

    # Must have executed 2 HTTP requests: filtered, then fallback
    assert len(recorded_requests) == 2
    assert recorded_requests[0]["filter"] == 'status: ANY("active", "draft") AND intent_family: ANY("shipping_policy")'
    assert recorded_requests[1]["filter"] == 'status: ANY("active", "draft")'

    assert res.fallback_triggered is True
    assert res.document_ids == ["faq-fallback-1", "faq-fallback-2", "faq-fallback-3"]
    assert res.filter_applied == 'status: ANY("active", "draft")'
