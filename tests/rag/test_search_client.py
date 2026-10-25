"""Offline unit tests for rag/search_client.py.

Tests cover:
- build_filter construction for status, intent, and effective-window
- SearchClient execution, filter propagation, and latency tracking (REQ-RAG-RETR-009)
- Intent family filtering and automatic unconstrained fallback (REQ-RAG-RETR-010)
"""

from datetime import datetime, timezone
import json
from search_client import (
    build_filter,
    clean_query,
    SearchClient,
    load_rich_faq_records,
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


# REQ-RAG-RETR-011
def test_search_client_soft_boost_condition(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({
            "results": [{"document": {"id": "faq-order-1"}}],
            "totalSize": 1,
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    res = client.search(
        "where is my late package",
        intent_family="order_status",
        intent_boost=0.5,
        allowed_statuses=["active", "draft"],
    )

    assert len(recorded_requests) == 1
    req = recorded_requests[0]
    # Soft boost: intent_family should NOT be in the filter
    assert "intent_family" not in req.get("filter", "")
    assert req.get("filter") == 'status: ANY("active", "draft")'
    # boostSpec should be populated
    assert "boostSpec" in req
    boost_specs = req["boostSpec"].get("conditionBoostSpecs", [])
    assert len(boost_specs) == 1
    assert boost_specs[0]["condition"] == 'intent_family: ANY("order_status")'
    assert boost_specs[0]["boost"] == 0.5
    assert res.document_ids == ["faq-order-1"]
    assert res.fallback_triggered is False


# REQ-RAG-RETR-011
def test_search_client_boost_spec_explicit(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({"results": [], "totalSize": 0})

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    custom_boost = {
        "conditionBoostSpecs": [{"condition": 'intent_family: ANY("other")', "boost": 0.8}]
    }
    client.search("custom boost query", boost_spec=custom_boost)

    assert len(recorded_requests) == 1
    assert recorded_requests[0].get("boostSpec") == custom_boost


# REQ-RAG-RETR-011
def test_search_client_ranking_expression(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({"results": [], "totalSize": 0})

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    client.search("test query", ranking_expression="semantic_similarity_score")

    assert len(recorded_requests) == 1
    assert recorded_requests[0].get("rankingExpression") == "semantic_similarity_score"


# REQ-RAG-RETR-011
def test_search_client_rank_method_offline(monkeypatch):
    recorded_requests = []
    recorded_urls = []

    def mock_urlopen(req, timeout=60):
        recorded_urls.append(req.full_url)
        body = json.loads(req.data.decode("utf-8"))
        recorded_requests.append(body)
        return MockHTTPResponse({
            "records": [
                {"id": "doc-2", "score": 0.85},
                {"id": "doc-1", "score": 0.42},
            ]
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    records_in = [
        {"id": "doc-1", "title": "Doc 1", "content": "Content 1"},
        {"id": "doc-2", "title": "Doc 2", "content": "Content 2"},
    ]
    reranked, lat_ms = client.rank("find doc", records=records_in, top_n=2)

    assert len(recorded_requests) == 1
    assert ":rank" in recorded_urls[0]
    assert recorded_requests[0]["model"] == "semantic-ranker-512@latest"
    assert recorded_requests[0]["topN"] == 2
    assert recorded_requests[0]["records"] == records_in
    assert [r["id"] for r in reranked] == ["doc-2", "doc-1"]
    assert lat_ms >= 0


# REQ-RAG-RETR-011
def test_search_client_managed_rerank_integration(monkeypatch):
    recorded_urls = []

    def mock_urlopen(req, timeout=60):
        recorded_urls.append(req.full_url)
        if ":rank" in req.full_url:
            return MockHTTPResponse({
                "records": [
                    {"id": "doc-3", "score": 0.9},
                    {"id": "doc-1", "score": 0.5},
                ]
            })
        # :search response
        return MockHTTPResponse({
            "results": [
                {
                    "document": {
                        "id": "doc-1",
                        "derivedStructData": {"title": "Title 1", "snippets": [{"snippet": "Snip 1"}]},
                    }
                },
                {
                    "document": {
                        "id": "doc-2",
                        "derivedStructData": {"title": "Title 2", "snippets": [{"snippet": "Snip 2"}]},
                    }
                },
                {
                    "document": {
                        "id": "doc-3",
                        "derivedStructData": {"title": "Title 3", "snippets": [{"snippet": "Snip 3"}]},
                    }
                },
            ],
            "totalSize": 3,
        })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    res = client.search(
        "where is order",
        page_size=20,
        rerank=True,
        rerank_top_n=2,
    )

    assert len(recorded_urls) == 2
    assert ":search" in recorded_urls[0]
    assert ":rank" in recorded_urls[1]
    assert res.rerank_applied is True
    # Reranker reordered to top 2: doc-3, doc-1
    assert res.document_ids == ["doc-3", "doc-1"]


# REQ-RAG-RETR-016
def test_load_rich_faq_records(tmp_path):
    doc_file = tmp_path / "faq-order-status-test.txt"
    doc_file.write_text(
        "# How to track my order\n\n"
        "Also asked as:\n"
        "- Where is my package?\n"
        "- How do I track delivery status?\n\n"
        "To track your order, visit Your Orders. Standard delivery takes 3-5 days.",
        encoding="utf-8",
    )
    records = load_rich_faq_records(docs_dir=tmp_path)
    assert "faq-order-status-test" in records
    rec = records["faq-order-status-test"]
    assert rec["id"] == "faq-order-status-test"
    assert rec["title"] == "How to track my order"
    assert "Also asked as:" in rec["content"]
    assert "- Where is my package?" in rec["content"]
    assert "To track your order, visit Your Orders." in rec["content"]


# REQ-RAG-RETR-016
def test_search_client_rich_rerank_blend_margin(monkeypatch):
    recorded_urls = []

    def mock_urlopen(req, timeout=60):
        url = req.full_url
        recorded_urls.append(url)
        if ":search" in url:
            return MockHTTPResponse({
                "results": [
                    {"document": {"id": "doc-orig-1", "structData": {"title": "Orig 1"}}},
                    {"document": {"id": "doc-alt-2", "structData": {"title": "Alt 2"}}},
                    {"document": {"id": "doc-alt-3", "structData": {"title": "Alt 3"}}},
                ],
                "totalSize": 3,
            })
        elif ":rank" in url:
            # Rank reorders doc-alt-2 to #1 with score 0.85, doc-orig-1 with score 0.78
            # Score delta = 0.85 - 0.78 = 0.07
            return MockHTTPResponse({
                "records": [
                    {"id": "doc-alt-2", "score": 0.85},
                    {"id": "doc-orig-1", "score": 0.78},
                    {"id": "doc-alt-3", "score": 0.50},
                ]
            })

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    client = SearchClient(token="mock-token")
    rich = {
        "doc-orig-1": {"id": "doc-orig-1", "title": "Orig 1", "content": "Rich content 1"},
        "doc-alt-2": {"id": "doc-alt-2", "title": "Alt 2", "content": "Rich content 2"},
    }

    # Case 1: blend_margin = 0.05. Delta is 0.07 > 0.05 -> reranker promotes doc-alt-2 to #1
    res1 = client.search(
        "query",
        rerank=True,
        blend_margin=0.05,
        rich_records=rich,
    )
    assert res1.document_ids == ["doc-alt-2", "doc-orig-1", "doc-alt-3"]

    # Case 2: blend_margin = 0.10. Delta is 0.07 <= 0.10 -> keep doc-orig-1 at #1!
    res2 = client.search(
        "query",
        rerank=True,
        blend_margin=0.10,
        rich_records=rich,
    )
    assert res2.document_ids == ["doc-orig-1", "doc-alt-2", "doc-alt-3"]


