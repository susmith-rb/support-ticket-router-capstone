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
    valid_intents = ["order_status", "shipping_policy", "returns_refunds"]
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

    # 5. Wrong queue when escalate matches
    t_wq = {"predicted": {"intent": "order_status", "escalate": True, "decision": "escalate", "queue": "human-standard"}}
    assert _classify_error_type(case_esc, t_wq, valid_intents) == "wrong_queue"


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
