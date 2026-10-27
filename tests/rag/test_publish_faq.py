import hashlib
import json

import pytest

from corpus.publish_faq import (
    answer_only,
    build_import_payload,
    check_manifest_invariants,
    lint,
    load_manifest,
    sha,
    stamp,
)


# REQ-RAG-CORPUS-005
def test_sha():
    text = "Hello Global Retail Hub"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sha(text) == expected


# REQ-RAG-CORPUS-005
def test_answer_only_drops_paraphrase_block():
    body = (
        "# Return Policy\n"
        "\n"
        "Also asked as:\n"
        "- Can I return this in 3 days?\n"
        "- When is my 2-day delivery arriving?\n"
        "\n"
        "Standard returns are accepted within 30 days of delivery."
    )
    res = answer_only(body)
    assert "Also asked as:" not in res
    assert "Can I return this in 3 days?" not in res
    assert "Standard returns are accepted within 30 days of delivery." in res
    assert "# Return Policy" in res


# REQ-RAG-CORPUS-006
def test_lint_flags_invented_fact():
    # "48 hours" is an invented fact not in SIMULATED_POLICY
    docs = {
        "faq-account-blocked": "# Account Locked\nSupport will respond within 48 hours to unblock your account."
    }
    bad = lint(docs)
    assert "faq-account-blocked" in bad
    assert "48 hour" in bad["faq-account-blocked"]


# REQ-RAG-CORPUS-006
def test_lint_flags_invented_days():
    # "7 days" is not in SIMULATED_POLICY
    docs = {
        "faq-claims": "# Claims\nYou must file a claim within 7 days."
    }
    bad = lint(docs)
    assert "faq-claims" in bad
    assert "7 day" in bad["faq-claims"]


# REQ-RAG-CORPUS-006
def test_lint_passes_policy_consistent_text():
    # Numbers that appear directly in SIMULATED_POLICY and are matched by NUM:
    # 30 days, 2 business days, 24 hours, 90 days, 35$
    docs = {
        "faq-returns": (
            "# Returns\n"
            "Returns: 30 days from delivery for most items.\n"
            "Duplicate charges are investigated within 2 business days.\n"
            "Free shipping on orders over 35$.\n"
            "Gift card refunds occur within 24 hours.\n"
            "Third-party claims must be filed within 90 days."
        )
    }
    bad = lint(docs)
    assert bad == {}


# REQ-RAG-CORPUS-006
def test_lint_range_parsing():
    # Both endpoints of policy ranges ("3-5 business days", "4-6 business days")
    # are recognized as allowed policy facts.
    docs = {
        "faq-range-3": "Refunds are issued within 3 business days.",
        "faq-range-4": "Standard delivery takes 4 business days.",
    }
    assert lint(docs) == {}


# REQ-RAG-CORPUS-006
def test_lint_dollar_amounts():
    # $35 is allowed by policy (free shipping over $35), while $50 is invented
    assert lint({"t": "Free over $35."}) == {}
    bad = lint({"t": "Costs $50 to ship."})
    assert "t" in bad
    assert "$50" in bad["t"]


# REQ-RAG-CORPUS-006
def test_lint_business_days():
    # 4 business days is within allowed policy (from 4-6 business days range),
    # while 7 business days is invented
    assert lint({"t": "Standard shipping is 4 business days."}) == {}
    bad = lint({"t": "Arrives in 7 business days."})
    assert "t" in bad
    assert "7 business day" in bad["t"]


# REQ-RAG-CORPUS-007
def test_load_manifest_nonexistent(tmp_path):
    assert load_manifest(tmp_path / "does_not_exist.jsonl") == {}


# REQ-RAG-CORPUS-007
def test_load_manifest_existing(tmp_path):
    p = tmp_path / "manifest.jsonl"
    entry = {"id": "faq-1", "structData": {"article_id": "faq-1", "version": 1}}
    p.write_text(json.dumps(entry) + "\n")
    res = load_manifest(p)
    assert "faq-1" in res
    assert res["faq-1"]["structData"]["version"] == 1


# REQ-RAG-CORPUS-008
def test_stamp_new_article():
    docs = {"faq-shipping": "# Shipping\nStandard delivery is 4-6 business days."}
    prev = {}
    today = "2026-10-23"
    prefix = "gs://bucket/corpus/v2"

    out, changes = stamp(docs, prev, prefix, today)

    assert changes["added"] == ["faq-shipping"]
    assert changes["changed"] == []
    assert changes["unchanged"] == []
    assert changes["retired"] == []

    assert len(out) == 1
    sd = out[0]["structData"]
    assert sd["article_id"] == "faq-shipping"
    assert sd["version"] == 1
    assert sd["status"] == "draft"
    assert sd["effective_from"] == today
    assert sd["content_sha256"] == sha(docs["faq-shipping"])


# REQ-RAG-CORPUS-008
def test_stamp_content_unchanged():
    content = "# Shipping\nStandard delivery is 4-6 business days."
    h = sha(content)
    today = "2026-10-23"
    prefix = "gs://bucket/corpus/v2"

    docs = {"faq-shipping": content}
    prev = {
        "faq-shipping": {
            "id": "faq-shipping",
            "structData": {
                "article_id": "faq-shipping",
                "version": 1,
                "status": "active",
                "content_sha256": h,
            },
            "content": {"uri": "gs://bucket/corpus/v1/docs/faq-shipping.txt"},
        }
    }

    out, changes = stamp(docs, prev, prefix, today)
    assert changes["unchanged"] == ["faq-shipping"]
    assert changes["changed"] == []
    sd = out[0]["structData"]
    assert sd["version"] == 1
    assert sd["status"] == "active"


# REQ-RAG-CORPUS-008
def test_stamp_content_changed():
    old_content = "# Shipping\nStandard delivery is 4-6 business days."
    new_content = "# Shipping\nStandard delivery is 3-5 business days."
    today = "2026-10-23"
    prefix = "gs://bucket/corpus/v2"

    docs = {"faq-shipping": new_content}
    prev = {
        "faq-shipping": {
            "id": "faq-shipping",
            "structData": {
                "article_id": "faq-shipping",
                "version": 1,
                "status": "active",
                "content_sha256": sha(old_content),
                "reviewed_by": "susmith",
                "reviewed_at": "2026-10-20",
            },
            "content": {"uri": "gs://bucket/corpus/v1/docs/faq-shipping.txt"},
        }
    }

    out, changes = stamp(docs, prev, prefix, today)
    assert changes["changed"] == ["faq-shipping@2"]
    sd = out[0]["structData"]
    assert sd["version"] == 2
    # status resets to draft upon content change
    assert sd["status"] == "draft"
    # reviews are wiped upon content change
    assert sd["reviewed_by"] == ""
    assert sd["reviewed_at"] == ""
    assert sd["content_sha256"] == sha(new_content)


# REQ-RAG-CORPUS-008
def test_stamp_retired_article():
    docs = {}  # article was deleted from docs/
    today = "2026-10-23"
    prefix = "gs://bucket/corpus/v2"

    prev = {
        "faq-obsolete": {
            "id": "faq-obsolete",
            "structData": {
                "article_id": "faq-obsolete",
                "version": 2,
                "status": "active",
                "effective_to": "",
            },
            "content": {"uri": "gs://bucket/corpus/v1/docs/faq-obsolete.txt"},
        }
    }

    out, changes = stamp(docs, prev, prefix, today)
    assert changes["retired"] == ["faq-obsolete"]
    assert len(out) == 1
    sd = out[0]["structData"]
    assert sd["status"] == "retired"
    assert sd["effective_to"] == today


# REQ-RAG-CORPUS-009
def test_build_import_payload_excludes_retired():
    manifest = [
        {"id": "faq-active", "structData": {"article_id": "faq-active", "status": "active"}},
        {"id": "faq-draft", "structData": {"article_id": "faq-draft", "status": "draft"}},
        {"id": "faq-retired", "structData": {"article_id": "faq-retired", "status": "retired"}},
    ]
    payload = build_import_payload(manifest)
    assert len(payload) == 2
    assert [m["id"] for m in payload] == ["faq-active", "faq-draft"]
    assert all(m["structData"]["status"] != "retired" for m in payload)
    # Ensure manifest was not mutated in place
    assert len(manifest) == 3


# REQ-RAG-CORPUS-009
def test_build_import_payload_unchanged_corpus_identical():
    manifest = [
        {"id": "faq-1", "structData": {"article_id": "faq-1", "status": "active"}},
        {"id": "faq-2", "structData": {"article_id": "faq-2", "status": "draft"}},
    ]
    payload = build_import_payload(manifest)
    assert payload == manifest


# REQ-RAG-CORPUS-007
def test_stamp_new_article_keeps_intent_family_from_build():
    docs = {"faq-order-status-late": "# Late Order\nYour order is delayed."}
    build_meta = {
        "faq-order-status-late": {
            "intent_family": "order_status",
            "theme": "Late Orders",
            "authored_by": "jetski-agent:gemini-3.8-flash-high",
            "n_source_messages": 42,
            "n_example_threads": 5,
        }
    }
    out, changes = stamp(docs, {}, "gs://bucket/corpus/v2", "2026-10-24", build_meta=build_meta, check_invariants=True)
    assert len(out) == 1
    sd = out[0]["structData"]
    assert sd["article_id"] == "faq-order-status-late"
    assert sd["intent_family"] == "order_status"
    assert sd["theme"] == "Late Orders"
    assert sd["authored_by"] == "jetski-agent:gemini-3.8-flash-high"
    assert sd["version"] == 1
    assert sd["status"] == "draft"


# REQ-RAG-CORPUS-007
def test_stamp_existing_article_refreshes_missing_intent_family():
    content = "# Late Order\nYour order is delayed."
    h = sha(content)
    docs = {"faq-order-status-late": content}
    prev = {
        "faq-order-status-late": {
            "id": "faq-order-status-late",
            "structData": {
                "article_id": "faq-order-status-late",
                "version": 1,
                "status": "draft",
                "content_sha256": h,
            },
            "content": {"uri": "gs://bucket/corpus/v1/docs/faq-order-status-late.txt"},
        }
    }
    build_meta = {
        "faq-order-status-late": {
            "intent_family": "order_status",
            "theme": "Late Orders",
            "authored_by": "jetski-agent:gemini-3.8-flash-high",
        }
    }
    out, changes = stamp(docs, prev, "gs://bucket/corpus/v2", "2026-10-24", build_meta=build_meta, check_invariants=True)
    assert changes["unchanged"] == ["faq-order-status-late"]
    assert changes["changed"] == []
    sd = out[0]["structData"]
    assert sd["version"] == 1
    assert sd["intent_family"] == "order_status"
    assert sd["theme"] == "Late Orders"
    assert sd["authored_by"] == "jetski-agent:gemini-3.8-flash-high"


# REQ-RAG-CORPUS-008
def test_check_manifest_invariants_valid():
    manifest = [
        {
            "id": "faq-account-payment-login",
            "structData": {
                "article_id": "faq-account-payment-login",
                "title": "Login Issues",
                "intent_family": "account_payment",
                "status": "draft",
            },
        }
    ]
    check_manifest_invariants(manifest)


# REQ-RAG-CORPUS-008
def test_check_manifest_invariants_fails_missing_intent():
    manifest = [
        {
            "id": "faq-test",
            "structData": {
                "article_id": "faq-test",
                "title": "Some Title",
                "status": "draft",
            },
        }
    ]
    with pytest.raises(ValueError, match="invalid or missing intent_family"):
        check_manifest_invariants(manifest)


# REQ-RAG-CORPUS-008
def test_check_manifest_invariants_fails_invalid_intent():
    manifest = [
        {
            "id": "faq-test",
            "structData": {
                "article_id": "faq-test",
                "title": "Some Title",
                "intent_family": "not_a_valid_family",
                "status": "draft",
            },
        }
    ]
    with pytest.raises(ValueError, match="invalid or missing intent_family"):
        check_manifest_invariants(manifest)


# REQ-RAG-CORPUS-008
def test_check_manifest_invariants_fails_empty_title():
    manifest = [
        {
            "id": "faq-test",
            "structData": {
                "article_id": "faq-test",
                "title": "",
                "intent_family": "order_status",
                "status": "draft",
            },
        }
    ]
    with pytest.raises(ValueError, match="empty title"):
        check_manifest_invariants(manifest)


# REQ-RAG-CORPUS-008
def test_check_manifest_invariants_skips_retired():
    manifest = [
        {
            "id": "faq-retired-doc",
            "structData": {
                "article_id": "faq-retired-doc",
                "status": "retired",
            },
        }
    ]
    check_manifest_invariants(manifest)


# REQ-RAG-CORPUS-010
def test_publish_faq_location_default_global(tmp_path, monkeypatch):
    import subprocess

    from corpus.publish_faq import main

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "faq-shipping.txt").write_text("# Shipping\nStandard delivery is 4-6 business days.", encoding="utf-8")

    recorded_calls = []

    def mock_call(method, url, project, body=None):
        recorded_calls.append((method, url, body))
        if "documents:import" in url:
            return {"name": "projects/susmith-fde-ticket-router/locations/global/operations/op123", "done": True, "response": {}}
        if ":search" in url:
            return {"results": [{"document": {"id": "faq-shipping", "structData": {"version": 1}}}]}
        if "op123" in url:
            return {"name": "op123", "done": True}
        return {}

    monkeypatch.setattr("corpus.publish_faq.call", mock_call)
    monkeypatch.setattr("corpus.publish_faq.get_access_token", lambda: "dummy-tok")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr("sys.argv", [
        "publish_faq.py",
        "--work", str(tmp_path),
        "--project", "susmith-fde-ticket-router",
    ])

    main()

    import_calls = [c for c in recorded_calls if "documents:import" in c[1]]
    assert len(import_calls) == 1
    assert import_calls[0][1] == (
        "https://discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/global/collections/default_collection/dataStores/faq-articles/branches/default_branch/documents:import"
    )

    search_calls = [c for c in recorded_calls if ":search" in c[1]]
    assert len(search_calls) == 1
    search_url_called, search_body = search_calls[0][1], search_calls[0][2]
    assert search_url_called == (
        "https://discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/global/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )
    assert search_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/dataStores/faq-articles"
    )


# REQ-RAG-CORPUS-010
def test_publish_faq_location_us(tmp_path, monkeypatch):
    import subprocess

    from corpus.publish_faq import main

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "faq-shipping.txt").write_text("# Shipping\nStandard delivery is 4-6 business days.", encoding="utf-8")

    recorded_calls = []

    def mock_call(method, url, project, body=None):
        recorded_calls.append((method, url, body))
        if "documents:import" in url:
            return {"name": "projects/susmith-fde-ticket-router/locations/us/operations/op456", "done": True, "response": {}}
        if ":search" in url:
            return {"results": [{"document": {"id": "faq-shipping", "structData": {"version": 1}}}]}
        if "op456" in url:
            return {"name": "op456", "done": True}
        return {}

    monkeypatch.setattr("corpus.publish_faq.call", mock_call)
    monkeypatch.setattr("corpus.publish_faq.get_access_token", lambda: "dummy-tok")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr("sys.argv", [
        "publish_faq.py",
        "--work", str(tmp_path),
        "--project", "susmith-fde-ticket-router",
        "--location", "us",
    ])

    main()

    import_calls = [c for c in recorded_calls if "documents:import" in c[1]]
    assert len(import_calls) == 1
    assert import_calls[0][1] == (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/dataStores/faq-articles/branches/default_branch/documents:import"
    )

    search_calls = [c for c in recorded_calls if ":search" in c[1]]
    assert len(search_calls) == 1
    search_url_called, search_body = search_calls[0][1], search_calls[0][2]
    assert search_url_called == (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/engines/support-kb-search/servingConfigs/default_search:search"
    )
    assert search_body["dataStoreSpecs"][0]["dataStore"] == (
        "projects/susmith-fde-ticket-router/locations/us/collections/default_collection/dataStores/faq-articles"
    )
