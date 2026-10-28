"""Unit tests for ground-truth provenance and verification status derivation.

# REQ-REVAL-003: Ground-truth provenance & verification policy.
"""

from __future__ import annotations

import dataclasses

from evals.routing.provenance import (
    PROVENANCE_STRENGTH,
    VALID_GROUND_TRUTH_SOURCES,
    derive_status,
    update_provenance,
)
from evals.routing.schema import (
    Adjudication,
    Annotation,
)


def test_provenance_strength_hierarchy():
    """# REQ-REVAL-003: PROVENANCE_STRENGTH defines strict hierarchical precedence."""
    assert PROVENANCE_STRENGTH[0] == "human_adjudicated"
    assert PROVENANCE_STRENGTH[1] == "human_consensus"
    assert PROVENANCE_STRENGTH[2] == "human_single"
    assert PROVENANCE_STRENGTH[3] == "teacher_consensus"
    assert PROVENANCE_STRENGTH[4] == "mapped_public"
    assert PROVENANCE_STRENGTH[5] == "llm_provisional"
    assert PROVENANCE_STRENGTH[6] == "synthetic_intended"
    assert PROVENANCE_STRENGTH[7] == "rule_derived"

    assert set(PROVENANCE_STRENGTH) == set(VALID_GROUND_TRUTH_SOURCES)


def test_derive_status_verified(sample_routing_cases):
    """# REQ-REVAL-003: human_adjudicated and human_consensus produce verified status."""
    case = sample_routing_cases[0]
    verified_case = dataclasses.replace(
        case,
        ground_truth_source="human_adjudicated",
        adjudication=None,
    )
    assert derive_status(verified_case) == "verified"

    consensus_case = dataclasses.replace(
        case,
        ground_truth_source="human_consensus",
        adjudication=None,
    )
    assert derive_status(consensus_case) == "verified"


def test_derive_status_provisional(sample_routing_cases):
    """# REQ-REVAL-003: synthetic, mapped, teacher, and llm sources stay provisional on their own."""
    case = sample_routing_cases[0]
    for unverified_source in (
        "synthetic_intended",
        "mapped_public",
        "teacher_consensus",
        "llm_provisional",
        "human_single",
        "rule_derived",
    ):
        test_case = dataclasses.replace(
            case,
            ground_truth_source=unverified_source,
            adjudication=None,
            annotations=(),
        )
        assert derive_status(test_case) == "provisional", f"{unverified_source} should derive provisional"


def test_derive_status_rejected(sample_routing_cases):
    """# REQ-REVAL-003: Case is rejected if adjudication verdict is 'reject'."""
    case = sample_routing_cases[0]
    rejected_case = dataclasses.replace(
        case,
        ground_truth_source="human_adjudicated",
        adjudication=Adjudication(
            adjudicator="annotator_alice",
            decided_at="2026-10-28T14:00:00Z",
            verdict="reject",
            rationale="Unintelligible spam text",
        ),
    )
    assert derive_status(rejected_case) == "rejected"


def test_derive_status_disputed(sample_routing_cases):
    """# REQ-REVAL-003: Disputed iff annotations disagree and no adjudication resolves it."""
    case = sample_routing_cases[0]
    disagreeing_anns = (
        Annotation(
            annotator_id="llm_pass_0",
            annotator_type="llm",
            labels={"intent": "order_status", "decision": "auto_rag", "queue": "none"},
        ),
        Annotation(
            annotator_id="llm_pass_1",
            annotator_type="llm",
            labels={"intent": "returns_refunds", "decision": "auto_rag", "queue": "none"},
        ),
    )

    disputed_case = dataclasses.replace(
        case,
        ground_truth_source="llm_provisional",
        annotations=disagreeing_anns,
        adjudication=None,
    )
    assert derive_status(disputed_case) == "disputed"

    # If adjudication resolves dispute with accept/relabel -> verified if human
    resolved_case = dataclasses.replace(
        disputed_case,
        ground_truth_source="human_adjudicated",
        adjudication=Adjudication(
            adjudicator="lead_adjudicator",
            decided_at="2026-10-28T15:00:00Z",
            verdict="accept",
            rationale="Resolved in favor of order_status per guidelines",
        ),
    )
    assert derive_status(resolved_case) == "verified"


def test_update_provenance(sample_routing_cases):
    """# REQ-REVAL-003: update_provenance returns a new instance with derived label_status."""
    case = sample_routing_cases[0]
    c_prov = dataclasses.replace(
        case,
        ground_truth_source="synthetic_intended",
        label_status="verified",  # incorrect manual setting
        adjudication=None,
        annotations=(),
    )
    updated = update_provenance(c_prov)
    assert updated.label_status == "provisional"
    assert updated.ticket_id == case.ticket_id
