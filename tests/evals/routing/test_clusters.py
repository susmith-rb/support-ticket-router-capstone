"""Unit tests for failure clusters and representative errors module.

# REQ-REVAL-017: Failure clusters, representative errors, explain CLI.
Validates error type taxonomy classification precedence, group key partitioning,
pure-Python TF-IDF agglomerative clustering, severity score weighting,
medoid representative selection, and top signature extraction.
"""

from __future__ import annotations

from evals.routing.clusters import _classify_error_type, cluster_failures
from evals.routing.schema import Expected, RoutingCase, Style


def _make_case(ticket_id: str, intent: str, decision: str, queue: str, description: str = "Order status query") -> RoutingCase:
    return RoutingCase(
        ticket_id=ticket_id,
        content_hash="h1",
        dataset_version="v1.0",
        split="dev",
        created_at="2026-10-28T12:00:00Z",
        subject=None,
        description=description,
        channel="web",
        language="en",
        pii_scrubbed=True,
        expected=Expected(
            intent=intent,
            acceptable_intents=(intent,),
            escalate=(decision == "escalate"),
            decision=decision,
            queue=queue,
        ),
        difficulty="medium",
        scenarios=("normal",),
        style=Style(register="casual", length_bucket="medium (20–80)"),
        source="synthetic",
        source_dataset="synthetic_jetski",
        source_ref="ref",
        ground_truth_source="human_adjudicated",
        label_status="verified",
        verified_fields=("intent", "decision", "queue", "escalate"),
        annotations=(),
        adjudication=None,
        group_id=f"grp-{ticket_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
    )


def test_error_type_precedence() -> None:
    # REQ-REVAL-017: Error classification precedence:
    # error > degraded > invalid_label > missed_escalation > wrong_queue > false_escalation > wrong_intent
    valid_intents = ["order_status", "shipping_policy", "returns_refunds", "account_payment", "other"]
    case_esc = _make_case("t1", "order_status", "escalate", "human-urgent")

    # 1. Error takes highest precedence
    t_err = {"error": "ConnectionRefused", "predicted": {"tier": "error", "degraded": True}}
    assert _classify_error_type(case_esc, t_err, valid_intents) == "error"

    # 2. Degraded takes precedence over invalid_label
    t_deg = {"predicted": {"degraded": True, "intent": "nonexistent_intent"}}
    assert _classify_error_type(case_esc, t_deg, valid_intents) == "degraded"

    # 3. Invalid label over missed_escalation
    t_inv = {"predicted": {"intent": "nonexistent_intent", "escalate": False, "decision": "auto_rag"}}
    assert _classify_error_type(case_esc, t_inv, valid_intents) == "invalid_label"

    # 4. Missed escalation over wrong_queue
    t_miss = {"predicted": {"intent": "order_status", "escalate": False, "decision": "auto_rag", "queue": "none"}}
    assert _classify_error_type(case_esc, t_miss, valid_intents) == "missed_escalation"

    # 4b. Missed escalation with reject_oos predicted
    t_miss_oos = {"predicted": {"intent": "order_status", "escalate": False, "decision": "reject_oos", "queue": "none"}}
    assert _classify_error_type(case_esc, t_miss_oos, valid_intents) == "missed_escalation"

    # 5. Wrong queue when escalate matches
    t_wq = {"predicted": {"intent": "order_status", "escalate": True, "decision": "escalate", "queue": "human-standard"}}
    assert _classify_error_type(case_esc, t_wq, valid_intents) == "wrong_queue"

    # 6. False escalation (expected auto_rag, predicted escalate)
    case_auto = _make_case("t_auto", "order_status", "auto_rag", "none")
    t_fe = {"predicted": {"intent": "order_status", "escalate": True, "decision": "escalate", "queue": "human-standard"}}
    assert _classify_error_type(case_auto, t_fe, valid_intents) == "false_escalation"

    # 7. false_oos (expected auto_rag, predicted reject_oos)
    t_foos = {"predicted": {"intent": "other", "escalate": False, "decision": "reject_oos", "queue": "none"}}
    assert _classify_error_type(case_auto, t_foos, valid_intents) == "false_oos"

    # 8. missed_oos (expected reject_oos, predicted auto_rag)
    case_oos = _make_case("t_oos", "other", "reject_oos", "none")
    t_moos = {"predicted": {"intent": "order_status", "escalate": False, "decision": "auto_rag", "queue": "none"}}
    assert _classify_error_type(case_oos, t_moos, valid_intents) == "missed_oos"


def test_cluster_failures_severity_ranking() -> None:
    # REQ-REVAL-017: Clusters ranked by score = size * severity_weight.
    # Case 1: missed_escalation (severity 5, size 1 -> score 5)
    c_miss = _make_case("t_miss", "order_status", "escalate", "human-urgent", description="Critical refund needed")
    t_miss = {
        "ticket_id": "t_miss",
        "predicted": {"intent": "order_status", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier1_router_s1"},
        "decision_raw": {"details": {"intent_prob": 0.82}},
    }

    # Cases 2 & 3: wrong_intent (severity 1, size 2 -> score 2)
    c_wi1 = _make_case("t_wi1", "order_status", "auto_rag", "none", description="Where is my delivery package")
    t_wi1 = {
        "ticket_id": "t_wi1",
        "predicted": {"intent": "shipping_policy", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier1_router_s1"},
        "decision_raw": {"details": {"intent_prob": 0.70}},
    }
    c_wi2 = _make_case("t_wi2", "order_status", "auto_rag", "none", description="Track delivery package status")
    t_wi2 = {
        "ticket_id": "t_wi2",
        "predicted": {"intent": "shipping_policy", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier1_router_s1"},
        "decision_raw": {"details": {"intent_prob": 0.75}},
    }

    clusters = cluster_failures([t_miss, t_wi1, t_wi2], [c_miss, c_wi1, c_wi2], cfg={"cluster_similarity_threshold": 0.10})
    assert len(clusters) == 2

    # Top cluster must be missed_escalation because score 5 > score 2
    top = clusters[0]
    assert top["cluster_id"] == 1
    assert top["error_type"] == "missed_escalation"
    assert top["severity_weight"] == 5
    assert top["score"] == 5

    # Second cluster is wrong_intent
    second = clusters[1]
    assert second["cluster_id"] == 2
    assert second["error_type"] == "wrong_intent"
    assert second["size"] == 2
    assert second["score"] == 2
    assert len(second["representatives"]) == 2
    assert second["representatives"][0]["tier"] == "tier1_router_s1"


def test_representative_misclassified_by_error_type() -> None:
    # REQ-REVAL-017: Top 3 representative misclassified tickets per error type.
    from evals.routing.clusters import get_representative_misclassified

    c_miss = _make_case("t_miss", "order_status", "escalate", "human-urgent")
    t_miss = {
        "ticket_id": "t_miss",
        "predicted": {"intent": "order_status", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier1_router_s1"},
    }
    c_wi = _make_case("t_wi", "order_status", "auto_rag", "none")
    t_wi = {
        "ticket_id": "t_wi",
        "predicted": {"intent": "shipping_policy", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier2_flash"},
    }

    reps = get_representative_misclassified([t_miss, t_wi], [c_miss, c_wi], n_per_type=3)
    assert "missed_escalation" in reps
    assert len(reps["missed_escalation"]) == 1
    assert reps["missed_escalation"][0]["ticket_id"] == "t_miss"
    assert reps["missed_escalation"][0]["tier"] == "tier1_router_s1"

    assert "wrong_intent" in reps
    assert len(reps["wrong_intent"]) == 1
    assert reps["wrong_intent"][0]["ticket_id"] == "t_wi"
    assert reps["wrong_intent"][0]["tier"] == "tier2_flash"


def test_key_level_clustering_defragmentation() -> None:
    # REQ-REVAL-017: Clusters ranked at KEY level (error_type, transition, deciding_tier)
    # Multiple invalid labels from tier2_flash should be grouped into a single key-level cluster.
    c1 = _make_case("t1", "order_status", "auto_rag", "none", description="Need order update")
    t1 = {
        "ticket_id": "t1",
        "predicted": {"intent": "shipping", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier2_flash"},
    }
    c2 = _make_case("t2", "account_payment", "auto_rag", "none", description="Payment problem")
    t2 = {
        "ticket_id": "t2",
        "predicted": {"intent": "billing", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier2_flash"},
    }
    c3 = _make_case("t3", "other", "auto_rag", "none", description="Technical glitch")
    t3 = {
        "ticket_id": "t3",
        "predicted": {"intent": "technical", "decision": "auto_rag", "escalate": False, "queue": "none", "tier": "tier2_flash"},
    }

    clusters = cluster_failures([t1, t2, t3], [c1, c2, c3])
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl["error_type"] == "invalid_label"
    assert cl["deciding_tier"] == "tier2_flash"
    assert cl["size"] == 3
    assert cl["transition"] == "*->shipping|billing|technical"
    assert len(cl["representatives"]) == 3


def test_get_error_type_summary() -> None:
    # REQ-REVAL-017: Error type summary table counts and shares
    from evals.routing.clusters import get_error_type_summary

    c1 = _make_case("t1", "order_status", "escalate", "human-urgent")
    t1 = {"ticket_id": "t1", "predicted": {"intent": "order_status", "decision": "auto_rag", "escalate": False, "tier": "tier1_router_s1"}}
    c2 = _make_case("t2", "order_status", "escalate", "human-urgent")
    t2 = {"ticket_id": "t2", "predicted": {"intent": "order_status", "decision": "auto_rag", "escalate": False, "tier": "tier1_router_s1"}}
    c3 = _make_case("t3", "account_payment", "auto_rag", "none")
    t3 = {"ticket_id": "t3", "predicted": {"intent": "billing", "decision": "auto_rag", "escalate": False, "tier": "tier2_flash"}}

    summary = get_error_type_summary([t1, t2, t3], [c1, c2, c3])
    assert len(summary) == 2
    # missed_escalation tier1_router_s1 (count 2, share 2/3)
    assert summary[0]["error_type"] == "missed_escalation"
    assert summary[0]["deciding_tier"] == "tier1_router_s1"
    assert summary[0]["count"] == 2
    assert summary[0]["share"] == 0.6667
    # invalid_label tier2_flash (count 1, share 1/3)
    assert summary[1]["error_type"] == "invalid_label"
    assert summary[1]["deciding_tier"] == "tier2_flash"
    assert summary[1]["count"] == 1
    assert summary[1]["share"] == 0.3333
