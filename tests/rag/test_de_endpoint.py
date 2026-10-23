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


def test_api_base_global_and_defaults():
    assert api_base("global") == "https://discoveryengine.googleapis.com/v1/"
    assert api_base() == "https://discoveryengine.googleapis.com/v1/"
    assert api_base("") == "https://discoveryengine.googleapis.com/v1/"
    assert api_base(None) == "https://discoveryengine.googleapis.com/v1/"


def test_api_base_regional_and_multi_region():
    assert api_base("us") == "https://us-discoveryengine.googleapis.com/v1/"
    assert api_base("eu") == "https://eu-discoveryengine.googleapis.com/v1/"
    assert api_base("us-central1") == "https://us-central1-discoveryengine.googleapis.com/v1/"


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


def test_data_store_name():
    assert (
        data_store_name("test-proj", "faq-articles", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/dataStores/faq-articles"
    )
    assert (
        data_store_name("test-proj", "faq-articles", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/dataStores/faq-articles"
    )


def test_branch_name():
    assert (
        branch_name("test-proj", "faq-articles", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/dataStores/faq-articles/branches/default_branch"
    )
    assert (
        branch_name("test-proj", "faq-articles", branch_id="0", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/dataStores/faq-articles/branches/0"
    )


def test_engine_name():
    assert (
        engine_name("test-proj", "support-kb-search", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search"
    )
    assert (
        engine_name("test-proj", "support-kb-search", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search"
    )


def test_serving_config_name():
    assert (
        serving_config_name("test-proj", "support-kb-search", location="global")
        == "projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search/servingConfigs/default_search"
    )
    assert (
        serving_config_name("test-proj", "support-kb-search", location="us")
        == "projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search/servingConfigs/default_search"
    )


def test_search_url():
    assert (
        search_url("test-proj", "support-kb-search", location="global")
        == "https://discoveryengine.googleapis.com/v1/projects/test-proj/locations/global/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )
    assert (
        search_url("test-proj", "support-kb-search", location="us")
        == "https://us-discoveryengine.googleapis.com/v1/projects/test-proj/locations/us/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )


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
