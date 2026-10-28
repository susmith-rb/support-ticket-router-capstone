"""Acceptance tests for human adjudication workflow.

# REQ-REVAL-007: Human adjudication workflow (Google Sheet)
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from evals.routing.adjudicate import (
    TSV_COLUMNS,
    export_queue,
    ingest_decisions,
)
from evals.routing.schema import (
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
)


def _make_provisional_case(
    ticket_id_suffix: str = "001",
    description: str = "Where is order [ORDER]?",
    intent: str = "order_status",
    decision: str = "auto_rag",
    queue: str = "none",
) -> RoutingCase:
    c_hash = compute_content_hash(subject=None, description=description)
    t_id = f"rt-bitext_cs-{c_hash[:8]}"
    return RoutingCase(
        ticket_id=t_id,
        content_hash=c_hash,
        dataset_version="routing_v1.0.0",
        split="pool",
        created_at="2026-10-28T00:00:00Z",
        subject=None,
        description=description,
        channel="chat",
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
        scenarios=("normal",),
        style=Style(register="casual", length_bucket="short (<20 words)"),
        source="public",
        source_dataset="bitext_cs",
        source_ref=f"ref_{ticket_id_suffix}",
        ground_truth_source="mapped_public",
        label_status="provisional",
        verified_fields=(),
        annotations=(),
        adjudication=None,
        group_id=f"grp-{t_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
        notes="",
    )


def test_export_queue_tsv(tmp_path: Path):
    """# REQ-REVAL-007: export_queue writes TSV with required headers and candidate values."""
    case = _make_provisional_case()
    tsv_file = tmp_path / "queue.tsv"

    count = export_queue([(case, "ambiguous_boundary")], tsv_file)
    assert count == 1
    assert tsv_file.exists()

    with tsv_file.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        assert reader.fieldnames == TSV_COLUMNS
        row = next(reader)
        assert row["ticket_id"] == case.ticket_id
        assert row["candidate_intent"] == "order_status"
        assert row["reason_queued"] == "ambiguous_boundary"


def test_ingest_decisions_accept_and_relabel(tmp_path: Path):
    """# REQ-REVAL-007: Ingesting accept and relabel decisions updates GT and status to verified."""
    case_1 = _make_provisional_case(ticket_id_suffix="1", description="Where is my shipment?")
    case_2 = _make_provisional_case(ticket_id_suffix="2", description="My card was charged twice.")

    tsv_file = tmp_path / "decisions.tsv"
    log_file = tmp_path / "adjudication_log.jsonl"

    rows = [
        {
            "ticket_id": case_1.ticket_id,
            "description": case_1.description,
            "subject": "",
            "candidate_intent": case_1.expected.intent,
            "candidate_decision": case_1.expected.decision,
            "candidate_queue": case_1.expected.queue,
            "candidate_sources": "mapped_public",
            "reason_queued": "review",
            "final_intent": "",
            "final_acceptable_intents": "",
            "final_escalate": "",
            "final_decision": "",
            "final_queue": "",
            "final_sentiment": "",
            "final_urgency": "",
            "verdict": "accept",
            "adjudicator": "alice@example.com",
            "rationale": "Confirmed order tracking request",
        },
        {
            "ticket_id": case_2.ticket_id,
            "description": case_2.description,
            "subject": "",
            "candidate_intent": case_2.expected.intent,
            "candidate_decision": case_2.expected.decision,
            "candidate_queue": case_2.expected.queue,
            "candidate_sources": "mapped_public",
            "reason_queued": "review",
            "final_intent": "account_payment",
            "final_acceptable_intents": "account_payment",
            "final_escalate": "true",
            "final_decision": "escalate",
            "final_queue": "human-standard",
            "final_sentiment": "angry",
            "final_urgency": "2",
            "verdict": "relabel",
            "adjudicator": "bob@example.com",
            "rationale": "Relabeled to account_payment with human escalation for duplicate charge",
        },
    ]

    with tsv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    updated, report = ingest_decisions(tsv_file, [case_1, case_2], log_path=log_file)
    assert len(updated) == 2
    assert report["processed_count"] == 2

    c1 = next(c for c in updated if c.ticket_id == case_1.ticket_id)
    assert c1.label_status == "verified"
    assert c1.ground_truth_source == "human_adjudicated"
    assert "intent" in c1.verified_fields

    c2 = next(c for c in updated if c.ticket_id == case_2.ticket_id)
    assert c2.label_status == "verified"
    assert c2.expected.intent == "account_payment"
    assert c2.expected.escalate is True
    assert c2.expected.queue == "human-standard"

    # Verify audit log appended
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_ingest_decisions_reject_and_unsure(tmp_path: Path):
    """# REQ-REVAL-007: Reject marks case rejected; unsure marks case disputed."""
    case_1 = _make_provisional_case(ticket_id_suffix="1", description="Gibberish asdfghjkl")
    case_2 = _make_provisional_case(ticket_id_suffix="2", description="Unclear query")

    tsv_file = tmp_path / "decisions_reject.tsv"
    rows = [
        {
            "ticket_id": case_1.ticket_id,
            "verdict": "reject",
            "adjudicator": "charlie@example.com",
            "rationale": "Incomprehensible gibberish",
        },
        {
            "ticket_id": case_2.ticket_id,
            "verdict": "unsure",
            "adjudicator": "charlie@example.com",
            "rationale": "Cannot determine intent with certainty",
        },
    ]

    with tsv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        for r in rows:
            full_r = {col: "" for col in TSV_COLUMNS}
            full_r.update(r)
            writer.writerow(full_r)

    updated, _ = ingest_decisions(tsv_file, [case_1, case_2], log_path=tmp_path / "audit.jsonl")
    c1 = next(c for c in updated if c.ticket_id == case_1.ticket_id)
    assert c1.label_status == "rejected"

    c2 = next(c for c in updated if c.ticket_id == case_2.ticket_id)
    assert c2.label_status == "disputed"


def test_ingest_two_independent_adjudicators_consensus(tmp_path: Path):
    """# REQ-REVAL-007: Two independent agreeing adjudicators promote to human_consensus."""
    case = _make_provisional_case(ticket_id_suffix="1", description="Where is my order?")

    tsv_1 = tmp_path / "dec_1.tsv"
    tsv_2 = tmp_path / "dec_2.tsv"
    log_file = tmp_path / "audit.jsonl"

    row_1 = {col: "" for col in TSV_COLUMNS}
    row_1.update({
        "ticket_id": case.ticket_id,
        "verdict": "accept",
        "adjudicator": "adjudicator_A",
        "rationale": "Agreed order status",
    })
    with tsv_1.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerow(row_1)

    # First pass: human_adjudicated
    updated_1, _ = ingest_decisions(tsv_1, [case], log_path=log_file)
    assert updated_1[0].ground_truth_source == "human_adjudicated"

    # Second pass by different adjudicator agreeing: promotes to human_consensus without supersede
    row_2 = {col: "" for col in TSV_COLUMNS}
    row_2.update({
        "ticket_id": case.ticket_id,
        "verdict": "accept",
        "adjudicator": "adjudicator_B",
        "rationale": "Second independent confirmation",
    })
    with tsv_2.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerow(row_2)

    updated_2, _ = ingest_decisions(tsv_2, updated_1, log_path=log_file, supersede=False)
    assert updated_2[0].ground_truth_source == "human_consensus"
    assert updated_2[0].label_status == "verified"


def test_ingest_idempotence_and_conflict_handling(tmp_path: Path):
    """# REQ-REVAL-007: Ingest is idempotent; conflicting re-decision raises unless supersede=True."""
    case = _make_provisional_case(ticket_id_suffix="1")
    tsv_file = tmp_path / "decisions.tsv"
    log_file = tmp_path / "audit.jsonl"

    row = {col: "" for col in TSV_COLUMNS}
    row.update({
        "ticket_id": case.ticket_id,
        "verdict": "accept",
        "adjudicator": "alice",
        "rationale": "Initial confirmation",
    })
    with tsv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerow(row)

    updated, _ = ingest_decisions(tsv_file, [case], log_path=log_file)

    # Re-importing same decision is a no-op
    updated_again, _ = ingest_decisions(tsv_file, updated, log_path=log_file)
    assert len(updated_again) == 1

    # Conflicting re-decision without supersede raises
    conflicting_tsv = tmp_path / "conflicting.tsv"
    conf_row = dict(row)
    conf_row["verdict"] = "reject"
    conf_row["rationale"] = "Changed mind to reject"
    with conflicting_tsv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerow(conf_row)

    with pytest.raises(ValueError, match="Conflicting adjudication"):
        ingest_decisions(conflicting_tsv, updated, supersede=False, log_path=log_file)

    # Succeeds with supersede=True
    updated_superseded, _ = ingest_decisions(conflicting_tsv, updated, supersede=True, log_path=log_file)
    assert updated_superseded[0].label_status == "rejected"
