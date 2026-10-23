import io
import json
import pytest
from de_endpoint import (
    api_base,
    collection_name,
    data_store_name,
    branch_name,
    engine_name,
    serving_config_name,
    search_url,
    documents_import_url,
)
import search_test


# REQ-RAG-RETR-004
def test_api_base_global_and_defaults():
    assert api_base("global") == "https://discoveryengine.googleapis.com/v1/"
    assert api_base() == "https://discoveryengine.googleapis.com/v1/"
    assert api_base("") == "https://discoveryengine.googleapis.com/v1/"
    assert api_base(None) == "https://discoveryengine.googleapis.com/v1/"


# REQ-RAG-RETR-004
def test_api_base_regional_and_multi_region():
    assert api_base("us") == "https://us-discoveryengine.googleapis.com/v1/"
    assert api_base("eu") == "https://eu-discoveryengine.googleapis.com/v1/"
    assert api_base("us-central1") == "https://us-central1-discoveryengine.googleapis.com/v1/"


# REQ-INFRA-SEARCH-005
def test_collection_name():
    assert (
        collection_name("test-proj", location="global")
        == "projects/test-proj/locations/global/collections/default_collection"
    )
    assert (
        collection_name("test-proj", location="us")
        == "projects/test-proj/locations/us/collections/default_collection"
    )
    assert (
        collection_name("test-proj", location="us", collection="custom_coll")
        == "projects/test-proj/locations/us/collections/custom_coll"
    )


# REQ-INFRA-SEARCH-005
def test_data_store_name():
    assert (
        data_store_name("test-proj", "faq-articles", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/dataStores/faq-articles"
    )
    assert (
        data_store_name("test-proj", "faq-articles", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/dataStores/faq-articles"
    )


# REQ-INFRA-SEARCH-005
def test_branch_name():
    assert (
        branch_name("test-proj", "faq-articles", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/dataStores/faq-articles/branches/default_branch"
    )
    assert (
        branch_name("test-proj", "faq-articles", branch_id="0", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/dataStores/faq-articles/branches/0"
    )


# REQ-INFRA-SEARCH-005
def test_engine_name():
    assert (
        engine_name("test-proj", "support-kb-search", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search"
    )
    assert (
        engine_name("test-proj", "support-kb-search", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search"
    )


# REQ-INFRA-SEARCH-005
def test_serving_config_name():
    assert (
        serving_config_name("test-proj", "support-kb-search", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search/servingConfigs/default_search"
    )
    assert (
        serving_config_name("test-proj", "support-kb-search", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search/servingConfigs/default_search"
    )


# REQ-RAG-RETR-004
def test_search_url():
    assert (
        search_url("test-proj", "support-kb-search", location="global")
        == "https://discoveryengine.googleapis.com/v1/projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )
    assert (
        search_url("test-proj", "support-kb-search", location="us")
        == "https://us-discoveryengine.googleapis.com/v1/projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )


# REQ-RAG-CORPUS-010
def test_documents_import_url():
    assert (
        documents_import_url("test-proj", "faq-articles", location="global")
        == "https://discoveryengine.googleapis.com/v1/projects/test-proj/locations/global/collections/default_collection/dataStores/faq-articles/branches/default_branch/documents:import"
    )
    assert (
        documents_import_url("test-proj", "faq-articles", location="us")
        == "https://us-discoveryengine.googleapis.com/v1/projects/test-proj/locations/us/collections/default_collection/dataStores/faq-articles/branches/default_branch/documents:import"
    )


class DummyHTTPResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# REQ-RAG-RETR-004
def test_search_test_location_default_global(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        recorded_requests.append((req.full_url, body))
        return DummyHTTPResponse({"results": [], "summary": {"summaryText": "Mock summary"}})

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "dummy-token\n")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("sys.argv", ["search_test.py", "return window query"])

    search_test.main()

    assert len(recorded_requests) == 2  # 1 for faq, 1 for threads
    faq_url, faq_body = recorded_requests[0]
    expected_url = (
        "https://discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/global/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    assert faq_url == expected_url
    assert faq_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/"
        "dataStores/faq-articles"
    )

    threads_url, threads_body = recorded_requests[1]
    assert threads_url == expected_url
    assert threads_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/"
        "dataStores/resolved-threads"
    )


# REQ-RAG-RETR-004
def test_search_test_location_us(monkeypatch):
    recorded_requests = []

    def mock_urlopen(req, timeout=60):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        recorded_requests.append((req.full_url, body))
        return DummyHTTPResponse({"results": [], "summary": {"summaryText": "Mock summary"}})

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "dummy-token\n")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("sys.argv", ["search_test.py", "--location", "us", "return window query"])

    search_test.main()

    assert len(recorded_requests) == 2
    faq_url, faq_body = recorded_requests[0]
    expected_url = (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    assert faq_url == expected_url
    assert faq_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/us/collections/default_collection/"
        "dataStores/faq-articles"
    )

    threads_url, threads_body = recorded_requests[1]
    assert threads_url == expected_url
    assert threads_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/us/collections/default_collection/"
        "dataStores/resolved-threads"
    )


# REQ-RAG-CORPUS-010
def test_import_threads_trigger_and_poll(monkeypatch):
    import import_threads

    calls = []

    def mock_call(method, url, project, token, body=None, timeout=120):
        calls.append((method, url, body))
        if method == "POST":
            return {"name": "projects/p/locations/us/collections/default_collection/dataStores/resolved-threads/branches/0/operations/op-123"}
        elif method == "GET":
            return {"name": "op-123", "done": True, "response": {"successCount": 81103}}
        return {}

    monkeypatch.setattr("import_threads.call", mock_call)
    monkeypatch.setattr("import_threads.get_token", lambda: "fake-tok")

    # Test trigger_import with location="us"
    res = import_threads.trigger_import(
        project="susmith-fde-ticket-router",
        location="us",
        data_store_id="resolved-threads",
        input_uri="gs://bucket/resolved.jsonl",
        reconciliation_mode="FULL",
        token="fake-tok",
    )
    assert "name" in res
    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST"
    assert url == (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/dataStores/resolved-threads/branches/default_branch/documents:import"
    )
    assert body["reconciliationMode"] == "FULL"
    assert body["gcsSource"]["inputUris"] == ["gs://bucket/resolved.jsonl"]

    # Test poll_operation
    op_res = import_threads.poll_operation("projects/p/locations/us/operations/op-123", location="us", token="fake-tok", poll_interval=0)
    assert op_res.get("done") is True
    assert op_res["response"]["successCount"] == 81103
    assert calls[1][0] == "GET"
    assert calls[1][1] == "https://us-discoveryengine.googleapis.com/v1/projects/p/locations/us/operations/op-123"

