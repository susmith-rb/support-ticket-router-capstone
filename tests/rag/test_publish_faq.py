import hashlib
import json
import pytest
from publish_faq import sha, answer_only, lint, load_manifest, stamp, build_import_payload


def test_sha():
    text = "Hello Global Retail Hub"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sha(text) == expected


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


def test_lint_flags_invented_fact():
    # "48 hours" is an invented fact not in SIMULATED_POLICY
    docs = {
        "faq-account-blocked": "# Account Locked\nSupport will respond within 48 hours to unblock your account."
    }
    bad = lint(docs)
    assert "faq-account-blocked" in bad
    assert "48 hour" in bad["faq-account-blocked"]


def test_lint_flags_invented_days():
    # "7 days" is not in SIMULATED_POLICY
    docs = {
        "faq-claims": "# Claims\nYou must file a claim within 7 days."
    }
    bad = lint(docs)
    assert "faq-claims" in bad
    assert "7 day" in bad["faq-claims"]


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


def test_lint_range_parsing():
    # Both endpoints of policy ranges ("3-5 business days", "4-6 business days")
    # are recognized as allowed policy facts.
    docs = {
        "faq-range-3": "Refunds are issued within 3 business days.",
        "faq-range-4": "Standard delivery takes 4 business days.",
    }
    assert lint(docs) == {}


def test_lint_dollar_amounts():
    # $35 is allowed by policy (free shipping over $35), while $50 is invented
    assert lint({"t": "Free over $35."}) == {}
    bad = lint({"t": "Costs $50 to ship."})
    assert "t" in bad
    assert "$50" in bad["t"]


def test_lint_business_days():
    # 4 business days is within allowed policy (from 4-6 business days range),
    # while 7 business days is invented
    assert lint({"t": "Standard shipping is 4 business days."}) == {}
    bad = lint({"t": "Arrives in 7 business days."})
    assert "t" in bad
    assert "7 business day" in bad["t"]


def test_load_manifest_nonexistent(tmp_path):
    assert load_manifest(tmp_path / "does_not_exist.jsonl") == {}


def test_load_manifest_existing(tmp_path):
    p = tmp_path / "manifest.jsonl"
    entry = {"id": "faq-1", "structData": {"article_id": "faq-1", "version": 1}}
    p.write_text(json.dumps(entry) + "\n")
    res = load_manifest(p)
    assert "faq-1" in res
    assert res["faq-1"]["structData"]["version"] == 1


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


def test_build_import_payload_unchanged_corpus_identical():
    manifest = [
        {"id": "faq-1", "structData": {"article_id": "faq-1", "status": "active"}},
        {"id": "faq-2", "structData": {"article_id": "faq-2", "status": "draft"}},
    ]
    payload = build_import_payload(manifest)
    assert payload == manifest
