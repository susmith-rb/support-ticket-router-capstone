"""Acceptance tests for provisional Gemini annotation and adjudication queueing.

# REQ-REVAL-006: Provisional Gemini annotation via Jetski (k-pass)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.routing.annotate import (
    export_packets,
    ingest,
    queue_for_adjudication,
    reliability_report,
)
from evals.routing.schema import (
    Annotation,
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
)
from ticket_router.config.taxonomy import load_taxonomy


def _make_case(
    ticket_id_suffix: str = "001",
    description: str = "Where is order [ORDER]?",
    intent: str = "order_status",
    decision: str = "auto_rag",
    queue: str = "none",
    scenarios: tuple[str, ...] = ("normal",),
    annotations: tuple[Annotation, ...] = (),
) -> RoutingCase:
    c_hash = compute_content_hash(subject=None, description=description)
    t_id = f"rt-kaggle_twcs-{c_hash[:8]}"
    return RoutingCase(
        ticket_id=t_id,
        content_hash=c_hash,
        dataset_version="routing_v1.0.0",
        split="dev",
        created_at="2026-10-28T00:00:00Z",
        subject=None,
        description=description,
        channel="twitter",
        language="en",
        pii_scrubbed=True,
        expected=Expected(
            intent=intent,
            acceptable_intents=(intent,),
            escalate=(decision == "escalate"),
            decision=decision,
            queue=queue,
            sentiment="neutral",
            urgency=1,
        ),
        difficulty="medium",
        scenarios=scenarios,
        style=Style(register="casual", length_bucket="short (<20 words)"),
        source="real",
        source_dataset="kaggle_twcs",
        source_ref=f"ref_{ticket_id_suffix}",
        ground_truth_source="mapped_public",
        label_status="provisional",
        verified_fields=(),
        annotations=annotations,
        adjudication=None,
        group_id=f"grp-{t_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
        notes="",
    )


def test_export_packets_k_pass(tmp_path: Path):
    """# REQ-REVAL-006: export_packets produces k packets per case with shuffled intents."""
    case = _make_case()
    pkt_file = tmp_path / "packets.jsonl"
    packets = export_packets([case], k=3, seed=20261028, out_path=pkt_file)

    assert len(packets) == 3
    assert pkt_file.exists()

    tax = load_taxonomy()
    expected_intents = set(tax.intents)

    for p in packets:
        assert p["ticket_id"] == case.ticket_id
        assert set(p["shuffled_intent_options"]) == expected_intents
        assert "output_schema" in p


def test_ingest_annotations_and_consensus(tmp_path: Path):
    """# REQ-REVAL-006: Ingest stores passes, computes consensus as llm_provisional, never verified."""
    case = _make_case()
    resp_file = tmp_path / "responses.jsonl"

    # 3 passes agreeing on order_status, one pass agreeing on returns_refunds
    responses = [
        {
            "ticket_id": case.ticket_id,
            "pass_index": 0,
            "labels": {
                "intent": "order_status",
                "acceptable_intents": ["order_status"],
                "escalate": False,
                "decision": "auto_rag",
                "queue": "none",
                "sentiment": "neutral",
                "urgency": 1,
            },
            "confidence": 0.95,
            "rationale": "Clear order tracking request",
        },
        {
            "ticket_id": case.ticket_id,
            "pass_index": 1,
            "labels": {
                "intent": "order_status",
                "acceptable_intents": ["order_status"],
                "escalate": False,
                "decision": "auto_rag",
                "queue": "none",
                "sentiment": "neutral",
                "urgency": 1,
            },
            "confidence": 0.90,
            "rationale": "Tracking request",
        },
        {
            "ticket_id": case.ticket_id,
            "pass_index": 2,
            "labels": {
                "intent": "returns_refunds",
                "acceptable_intents": ["returns_refunds"],
                "escalate": False,
                "decision": "auto_rag",
                "queue": "none",
                "sentiment": "neutral",
                "urgency": 1,
            },
            "confidence": 0.60,
            "rationale": "Possible return mention",
        },
    ]

    with resp_file.open("w", encoding="utf-8") as f:
        for r in responses:
            f.write(json.dumps(r) + "\n")

    updated_cases, report = ingest(resp_file, [case])
    assert len(updated_cases) == 1
    updated = updated_cases[0]

    # Consensus label is order_status (2 out of 3 votes)
    assert updated.expected.intent == "order_status"
    # Ground truth source is strictly provisional, never verified
    assert updated.ground_truth_source == "llm_provisional"
    assert updated.label_status == "provisional"
    assert updated.verified_fields == ()
    assert len(updated.annotations) == 3


def test_queue_for_adjudication_triggers():
    """# REQ-REVAL-006: queue_for_adjudication flags disagreement, safety shifts, and ambiguous scenarios."""
    # 1. Clean unanimous case -> should NOT queue
    clean_ann = (
        Annotation(annotator_id="pass_0", annotator_type="llm", labels={"intent": "order_status", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=0),
        Annotation(annotator_id="pass_1", annotator_type="llm", labels={"intent": "order_status", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=1),
    )
    clean_case = _make_case(ticket_id_suffix="clean", annotations=clean_ann)
    queued = queue_for_adjudication([clean_case])
    assert len(queued) == 0

    # 2. Ambiguous scenario -> queues automatically
    ambig_case = _make_case(ticket_id_suffix="ambig", scenarios=("ambiguous",), annotations=clean_ann)
    queued_ambig = queue_for_adjudication([ambig_case])
    assert len(queued_ambig) == 1
    assert "ambiguous" in queued_ambig[0][1]

    # 3. Disagreement across passes -> queues
    disagree_ann = (
        Annotation(annotator_id="pass_0", annotator_type="llm", labels={"intent": "order_status", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=0),
        Annotation(annotator_id="pass_1", annotator_type="llm", labels={"intent": "returns_refunds", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=1),
    )
    disagree_case = _make_case(ticket_id_suffix="disagree", annotations=disagree_ann)
    queued_disagree = queue_for_adjudication([disagree_case])
    assert len(queued_disagree) == 1
    assert "disagree on intent" in queued_disagree[0][1]

    # 4. Safety field modal share < 1.0 (escalate split)
    safety_ann = (
        Annotation(annotator_id="pass_0", annotator_type="llm", labels={"intent": "other", "decision": "escalate", "queue": "human-standard", "escalate": True}, pass_index=0),
        Annotation(annotator_id="pass_1", annotator_type="llm", labels={"intent": "other", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=1),
    )
    safety_case = _make_case(ticket_id_suffix="safety", annotations=safety_ann)
    queued_safety = queue_for_adjudication([safety_case])
    assert len(queued_safety) == 1


def test_reliability_report_stats_integration():
    """# REQ-REVAL-006: reliability_report integration with evals.routing.stats when available."""
    pytest.importorskip("evals.routing.stats")
    # If stats module is installed, verify reliability metrics
    ann = (
        Annotation(annotator_id="pass_0", annotator_type="llm", labels={"intent": "order_status", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=0),
        Annotation(annotator_id="pass_1", annotator_type="llm", labels={"intent": "order_status", "decision": "auto_rag", "queue": "none", "escalate": False}, pass_index=1),
    )
    case = _make_case(annotations=ann)
    rep = reliability_report([case])
    assert "fields" in rep
    assert "intent" in rep["fields"]
    assert "alpha" in rep["fields"]["intent"]
