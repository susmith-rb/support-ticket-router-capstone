"""Unit tests for deduplication, MinHash clustering, and leakage detection.

# REQ-REVAL-008: Deduplication & leakage control.
"""

from __future__ import annotations

import dataclasses

from evals.routing.dedup import (
    check_leakage,
    cluster_near_duplicates,
    deduplicate_and_cluster,
    exact_deduplicate,
)
from evals.routing.schema import (
    FailureRef,
)


def test_exact_deduplicate_provenance_tiebreak(sample_routing_cases):
    """# REQ-REVAL-008: Exact duplicates are dropped, keeping strongest provenance and lowest ticket_id."""
    base = sample_routing_cases[0]

    # Create two duplicate cases with different provenance
    dup_teacher = dataclasses.replace(
        base,
        ticket_id="rt-kaggle_twcs-teacher01",
        ground_truth_source="teacher_consensus",
        label_status="provisional",
    )
    dup_human = dataclasses.replace(
        base,
        ticket_id="rt-kaggle_twcs-human001",
        ground_truth_source="human_adjudicated",
        label_status="verified",
    )
    dup_synth = dataclasses.replace(
        base,
        ticket_id="rt-kaggle_twcs-synth001",
        ground_truth_source="synthetic_intended",
        label_status="provisional",
    )

    retained, dropped = exact_deduplicate([dup_teacher, dup_synth, dup_human])
    assert dropped == 2
    assert len(retained) == 1
    assert retained[0].ticket_id == "rt-kaggle_twcs-human001"
    assert retained[0].ground_truth_source == "human_adjudicated"


def test_near_duplicate_minhash_clustering(sample_routing_cases):
    """# REQ-REVAL-008: MinHash + Jaccard >= 0.85 assigns consistent group_id to near-duplicates."""
    # Find the near-duplicate pair in fixtures
    near_dups = [c for c in sample_routing_cases if c.group_id == "grp-near-dup-order"]
    assert len(near_dups) == 2

    clustered, report = cluster_near_duplicates(sample_routing_cases, seed=20261028)
    assert report["near_duplicate_clusters"] >= 1

    # Check that both cases share the same group_id
    c1 = next(c for c in clustered if c.ticket_id == near_dups[0].ticket_id)
    c2 = next(c for c in clustered if c.ticket_id == near_dups[1].ticket_id)
    assert c1.group_id == c2.group_id == "grp-near-dup-order"


def test_deduplicate_and_cluster_pipeline(sample_routing_cases):
    """# REQ-REVAL-008: End-to-end deduplicate_and_cluster returns retained cases and counts."""
    base = sample_routing_cases[0]
    duplicate = dataclasses.replace(base, ticket_id="rt-kaggle_twcs-dup99999")
    cases = list(sample_routing_cases) + [duplicate]

    retained, report = deduplicate_and_cluster(cases, seed=20261028)
    assert report["input_cases"] == len(cases)
    assert report["exact_duplicates_dropped"] == 1
    assert report["retained_cases"] == len(sample_routing_cases)
    assert len(retained) == len(sample_routing_cases)


def test_check_leakage_detection(sample_routing_cases):
    """# REQ-REVAL-008: check_leakage catches exact, near-duplicate, and group_id leakage."""
    dev_cases = [sample_routing_cases[0]]
    # 1. Exact text duplicate leaked into heldout
    leaked_heldout = [
        dataclasses.replace(
            sample_routing_cases[0],
            ticket_id="rt-synthetic_jetski-leak1",
            split="heldout",
        )
    ]
    splits = {"dev": dev_cases, "heldout": leaked_heldout}
    violations = check_leakage(splits)
    assert len(violations) >= 1
    assert any("Exact normalized text leakage" in v[2] or "Group ID" in v[2] for v in violations)

    # 2. Deliberate regression copy allowed in regression vs dev
    reg_copy = dataclasses.replace(
        sample_routing_cases[0],
        ticket_id="rt-observed_failure-reg001",
        split="regression",
        failure_ref=FailureRef(
            failure_id="REG-0005",
            observed_at="2026-10-28T00:00:00Z",
            symptom="Documented test failure",
            root_cause="Root cause analysis",
        ),
    )
    splits_regression = {"dev": dev_cases, "regression": [reg_copy]}
    assert check_leakage(splits_regression) == []

    # 3. Deliberate regression copy strictly forbidden from heldout
    splits_heldout_regression = {"heldout": [reg_copy], "dev": dev_cases}
    assert len(check_leakage(splits_heldout_regression)) >= 1
