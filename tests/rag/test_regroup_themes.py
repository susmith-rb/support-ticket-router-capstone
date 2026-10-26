"""tests/rag/test_regroup_themes.py — unit tests for FAQ theme regrouping and gold map generation."""

import json
import pathlib
import pytest

from corpus.regroup_themes import (
    DEFAULT_DROPPED,
    ORDER_STATUS_MERGES,
    apply_merges,
    build_gold_map,
    cluster_other_topics,
    doc_id_for_theme,
    reassign_other_threads,
    regroup,
    validate_v2,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _make_mock_v1_themes():
    """Create a minimal realistic set of v1 themes covering merges, drops, and unchanged themes."""
    return [
        # Merged group 1: Late / not arrived
        {
            "theme_id": "late_general_delivery",
            "title": "Late or Delayed General Delivery",
            "description": "General delayed delivery issues",
            "intent_family": "order_status",
            "topics": ["delayed package", "late delivery"],
            "thread_ids": ["t1", "t2"],
        },
        {
            "theme_id": "late_prime_delivery",
            "title": "Late Prime Delivery",
            "description": "Late prime orders",
            "intent_family": "order_status",
            "topics": ["late prime delivery", "prime 2 day delay"],
            "thread_ids": ["t3"],
        },
        {
            "theme_id": "general_order_not_arrived",
            "title": "Order Not Arrived (General)",
            "description": "General non-arrival",
            "intent_family": "order_status",
            "topics": ["order not arrived", "where is my stuff"],
            "thread_ids": ["t4"],
        },
        {
            "theme_id": "order_not_shipped",
            "title": "Order Not Shipped or Dispatched",
            "description": "Stuck in processing",
            "intent_family": "order_status",
            "topics": ["not yet shipped", "stuck in dispatch"],
            "thread_ids": ["t5"],
        },
        # Merged group 2: Missing delivered / stolen
        {
            "theme_id": "missing_delivered_package",
            "title": "Missing Package Marked as Delivered",
            "description": "Marked delivered but missing",
            "intent_family": "order_status",
            "topics": ["marked delivered but missing"],
            "thread_ids": ["t6", "t7"],
        },
        {
            "theme_id": "stolen_packages",
            "title": "Stolen Packages",
            "description": "Porch piracy reports",
            "intent_family": "order_status",
            "topics": ["stolen from porch"],
            "thread_ids": ["t8"],
        },
        # Dropped praise/noise themes
        {
            "theme_id": "positive_feedback",
            "title": "Positive Feedback and Early Deliveries",
            "description": "Compliments",
            "intent_family": "order_status",
            "topics": ["thanks for fast delivery"],
            "thread_ids": ["t9"],
        },
        {
            "theme_id": "customer_service_positive_feedback",
            "title": "Customer Service Positive Feedback",
            "description": "Service praise",
            "intent_family": "other",
            "topics": ["great support agent"],
            "thread_ids": ["t10"],
        },
        {
            "theme_id": "social_media_and_sharing",
            "title": "Social Media and Sharing",
            "description": "Chatter and jokes",
            "intent_family": "other",
            "topics": ["funny tweet", "meme"],
            "thread_ids": ["t11"],
        },
        # Unchanged themes
        {
            "theme_id": "shipping_costs_and_fees",
            "title": "Shipping Costs & Fees",
            "description": "Questions about shipping fees",
            "intent_family": "shipping_policy",
            "topics": ["shipping fee question"],
            "thread_ids": ["t12", "t13"],
        },
        {
            "theme_id": "wrong_item_received",
            "title": "Wrong Item Received",
            "description": "Incorrect item in delivery",
            "intent_family": "returns_refunds",
            "topics": ["received wrong item"],
            "thread_ids": ["t14"],
        },
        {
            "theme_id": "account_login_issues",
            "title": "Account Login & Password Issues",
            "description": "Login problems",
            "intent_family": "account_payment",
            "topics": ["cannot log in", "reset password"],
            "thread_ids": ["t15"],
        },
        {
            "theme_id": "prime_video_and_streaming",
            "title": "Prime Video and Streaming",
            "description": "Streaming inquiries",
            "intent_family": "other",
            "topics": ["video streaming issue", "episode missing"],
            "thread_ids": ["t16", "t17"],
        },
    ]


# REQ-RAG-CORPUS-011
def test_doc_id_for_theme():
    assert doc_id_for_theme("order_status", "late_general_delivery") == "faq-order-status-late-general-delivery"
    assert doc_id_for_theme({"intent_family": "other", "theme_id": "app_and_website_issues"}) == "faq-other-app-and-website-issues"
    assert doc_id_for_theme("shipping_policy", "prime_delivery_speed_&_delays") == "faq-shipping-policy-prime-delivery-speed---delays"


# REQ-RAG-CORPUS-012
def test_merge_application():
    v1_themes = _make_mock_v1_themes()
    updated_themes, merge_map = apply_merges(v1_themes, ORDER_STATUS_MERGES)

    # Verify source themes removed
    updated_doc_ids = {doc_id_for_theme(th) for th in updated_themes}
    assert "faq-order-status-late-general-delivery" not in updated_doc_ids
    assert "faq-order-status-late-prime-delivery" not in updated_doc_ids
    assert "faq-order-status-general-order-not-arrived" not in updated_doc_ids
    assert "faq-order-status-order-not-shipped" not in updated_doc_ids
    assert "faq-order-status-missing-delivered-package" not in updated_doc_ids
    assert "faq-order-status-stolen-packages" not in updated_doc_ids

    # Verify target merged themes exist
    assert "faq-order-status-late-or-not-arrived-order" in updated_doc_ids
    assert "faq-order-status-missing-or-stolen-delivered-package" in updated_doc_ids

    # Verify merge map
    assert merge_map["faq-order-status-late-general-delivery"] == "faq-order-status-late-or-not-arrived-order"
    assert merge_map["faq-order-status-late-prime-delivery"] == "faq-order-status-late-or-not-arrived-order"
    assert merge_map["faq-order-status-missing-delivered-package"] == "faq-order-status-missing-or-stolen-delivered-package"
    assert merge_map["faq-order-status-stolen-packages"] == "faq-order-status-missing-or-stolen-delivered-package"

    # Verify merged theme properties
    late_merged = next(th for th in updated_themes if th["theme_id"] == "late_or_not_arrived_order")
    assert late_merged["intent_family"] == "order_status"
    assert late_merged["title"] == "Late or Not-Arrived Orders"
    # Thread ids should be union of t1, t2, t3, t4, t5 without duplicates
    assert set(late_merged["thread_ids"]) == {"t1", "t2", "t3", "t4", "t5"}
    assert len(late_merged["thread_ids"]) == 5
    # Topics unioned
    assert "delayed package" in late_merged["topics"]
    assert "not yet shipped" in late_merged["topics"]

    missing_merged = next(th for th in updated_themes if th["theme_id"] == "missing_or_stolen_delivered_package")
    assert set(missing_merged["thread_ids"]) == {"t6", "t7", "t8"}


# REQ-RAG-CORPUS-013
def test_gold_map_completeness():
    v1_themes = _make_mock_v1_themes()
    updated_themes, merge_map = apply_merges(v1_themes, ORDER_STATUS_MERGES)

    # Filter out dropped themes from v2
    v2_themes = [th for th in updated_themes if doc_id_for_theme(th) not in DEFAULT_DROPPED]

    gold_map = build_gold_map(v1_themes, v2_themes, merge_map, dropped_dict=DEFAULT_DROPPED)

    # Verify every v1 doc id is either mapped or dropped with a reason
    for th in v1_themes:
        v1_id = doc_id_for_theme(th)
        if v1_id in DEFAULT_DROPPED:
            assert v1_id in gold_map["dropped"]
            assert len(gold_map["dropped"][v1_id].strip()) > 0
        else:
            # Top level lookup must return v2 doc id
            v2_id = gold_map.get(v1_id, v1_id)
            assert v2_id.startswith("faq-")
            assert v2_id in {doc_id_for_theme(t) for t in v2_themes}

    # Verify validate_v2 succeeds
    validate_v2(v1_themes, v2_themes, gold_map)


# REQ-RAG-CORPUS-013
def test_gold_map_unmapped_theme_raises():
    v1_themes = _make_mock_v1_themes()
    # If a v1 theme is missing from v2 and not in dropped_dict, build_gold_map must fail
    v2_themes = []  # empty v2 themes
    with pytest.raises(ValueError, match="neither mapped, dropped, nor found"):
        build_gold_map(v1_themes, v2_themes, {}, dropped_dict={})


# REQ-RAG-CORPUS-012
def test_no_thread_assigned_to_two_v2_themes():
    v1_themes = _make_mock_v1_themes()
    v2_themes, merge_map = apply_merges(v1_themes, ORDER_STATUS_MERGES)
    gold_map = build_gold_map(v1_themes, v2_themes, merge_map, dropped_dict=DEFAULT_DROPPED)

    # Clean case passes
    validate_v2(v1_themes, v2_themes, gold_map)

    # Introduce a duplicate thread assignment
    corrupt_v2 = [dict(th) for th in v2_themes]
    # Assign thread 't1' (already in late_or_not_arrived_order) to another theme as well
    corrupt_v2[-1]["thread_ids"] = list(corrupt_v2[-1]["thread_ids"]) + ["t1"]

    with pytest.raises(ValueError, match="Invariant violated: thread 't1' is assigned to multiple v2 themes"):
        validate_v2(v1_themes, corrupt_v2, gold_map)


# REQ-RAG-CORPUS-012
def test_reassign_other_threads_disjointness():
    clustered_themes = [
        {"theme_id": "kindle_e_readers", "topics": ["kindle book", "ebook download"]},
        {"theme_id": "fire_tv", "topics": ["fire stick", "tv streaming", "ebook download"]},  # duplicate topic across clusters
    ]
    # Thread t1 has topic 'ebook download' which matches both themes
    other_msgs = [
        ("thread_1", "ebook download"),
        ("thread_2", "fire stick"),
        ("thread_3", "kindle book"),
    ]
    assigned = reassign_other_threads(clustered_themes, other_msgs)

    # Verify thread_1 was assigned to exactly one theme
    t1_count = sum(1 for th in assigned if "thread_1" in th["thread_ids"])
    assert t1_count == 1

    # Verify no thread is assigned twice
    all_assigned = [tid for th in assigned for tid in th["thread_ids"]]
    assert len(all_assigned) == len(set(all_assigned))


# REQ-RAG-CORPUS-014
def test_cluster_other_injectable_fake_llm():
    other_msgs = [
        ("t101", "kindle battery"),
        ("t102", "kindle screen freeze"),
        ("t103", "kindle paperwhite issue"),
        ("t104", "kindle book not loading"),
        ("t105", "kindle sync failure"),
        ("t106", "kindle reset help"),
        ("t107", "fire tv remote issue"),  # smaller than min_theme_size
    ]

    def fake_cluster_fn(prompt, schema):
        assert "Group the answerable customer support topics" in prompt
        assert schema["type"] == "object"
        res = {
            "themes": [
                {
                    "theme_id": "kindle_e_readers",
                    "title": "Kindle e-Readers and E-Books",
                    "description": "Hardware and sync issues for Kindle devices",
                    "topics": [
                        "kindle battery",
                        "kindle screen freeze",
                        "kindle paperwhite issue",
                        "kindle book not loading",
                        "kindle sync failure",
                        "kindle reset help",
                    ],
                },
                {
                    "theme_id": "fire_tv",
                    "title": "Fire TV Devices",
                    "description": "Fire TV issues",
                    "topics": ["fire tv remote issue"],
                },
            ]
        }
        usage = {"prompt_token_count": 120, "candidates_token_count": 45}
        return res, usage

    themes, usage = cluster_other_topics(other_msgs, cluster_fn=fake_cluster_fn, min_theme_size=6)

    # fire_tv had only 1 thread (< 6), so only kindle_e_readers should survive
    assert len(themes) == 1
    assert themes[0]["theme_id"] == "kindle_e_readers"
    assert len(themes[0]["thread_ids"]) == 6
    assert usage["prompt_token_count"] == 120


# REQ-RAG-CORPUS-012
def test_regroup_full_pipeline_with_real_v1_file():
    v1_file = REPO_ROOT / "rag" / "results" / "themes_v1.json"
    if not v1_file.exists():
        pytest.skip("themes_v1.json not found in repo")

    v1_themes = json.load(open(v1_file))
    assert len(v1_themes) == 56

    # Run offline regroup
    v2_themes, gold_map, usage = regroup(v1_themes)

    # In v1: 56 themes
    # 5 dropped: 3 praise + 2 noise (social_media_and_sharing, general_complaints_and_feedback)
    # Merges:
    # 4 late/undelivered -> 1 (-3)
    # 2 missing/stolen -> 1 (-1)
    # Expected v2 count: 56 - 5 - 3 - 1 = 47 themes
    assert len(v2_themes) == 47

    # Validate completeness and thread disjointness
    validate_v2(v1_themes, v2_themes, gold_map)

    # Check that gold_map has all 56 v1 doc ids accounted for
    for th in v1_themes:
        did = doc_id_for_theme(th)
        if did in DEFAULT_DROPPED:
            assert did in gold_map["dropped"]
        else:
            assert did in gold_map["map"]
            assert gold_map.get(did, did) in {doc_id_for_theme(t) for t in v2_themes}
