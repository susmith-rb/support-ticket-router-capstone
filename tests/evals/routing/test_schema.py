"""Unit tests for RoutingCase schema v1, serialization, and text normalization.

# REQ-REVAL-002: Routing case JSONL schema v1.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from evals.routing.schema import (
    RoutingCase,
    compute_content_hash,
    make_ticket_id,
    normalize_text,
    read_jsonl,
    validate_case,
    write_jsonl,
)


def test_normalize_text():
    """# REQ-REVAL-002: normalize_text strips @mentions, URLs, redactions and collapses whitespace."""
    raw = "  Hello @AmazonHelp! Check https://example.com/track?id=123 for [ORDER] and [EMAIL] right now.   "
    normalized = normalize_text(raw)
    assert "@" not in normalized
    assert "https://" not in normalized
    assert "[order]" not in normalized
    assert "[email]" not in normalized
    assert normalized == "hello ! check for and right now."

    # Unicode NFKC test
    fullwidth = "Ｈｅｌｌｏ"
    assert normalize_text(fullwidth) == "hello"


def test_content_hash_and_ticket_id():
    """# REQ-REVAL-002: compute_content_hash and make_ticket_id are deterministic."""
    h1 = compute_content_hash("Subject line", "Description text")
    h2 = compute_content_hash("  SUBJECT line  ", "description  TEXT  ")
    assert h1 == h2
    assert len(h1) == 64

    tid = make_ticket_id("kaggle_twcs", h1)
    assert tid == f"rt-kaggle_twcs-{h1[:8]}"


def test_routing_case_frozen_and_roundtrip(sample_routing_cases):
    """# REQ-REVAL-002: RoutingCase dataclasses are immutable and roundtrip through dict cleanly."""
    case = sample_routing_cases[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.ticket_id = "new_id"  # type: ignore

    d = case.to_dict()
    # Check canonical key order
    expected_keys = [
        "schema_version",
        "ticket_id",
        "content_hash",
        "dataset_version",
        "split",
        "created_at",
        "subject",
        "description",
        "channel",
        "language",
        "pii_scrubbed",
        "expected",
        "difficulty",
        "scenarios",
        "style",
        "source",
        "source_dataset",
        "source_ref",
        "ground_truth_source",
        "label_status",
        "verified_fields",
        "annotations",
        "adjudication",
        "group_id",
        "parent_ticket_id",
        "perturbation",
        "failure_ref",
        "notes",
    ]
    assert list(d.keys()) == expected_keys

    reconstructed = RoutingCase.from_dict(d)
    assert reconstructed == case


def test_validate_case_success_and_failures(sample_routing_cases):
    """# REQ-REVAL-002: validate_case validates all constraints and catches invalid fields."""
    for case in sample_routing_cases:
        errs = validate_case(case)
        assert not errs, f"Case {case.ticket_id} failed validation: {errs}"

    # Invalid ticket_id
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["ticket_id"] = "bad-id"
    errs = validate_case(bad_dict)
    assert any("does not match expected format" in e for e in errs)

    # Empty description
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["description"] = "   "
    errs = validate_case(bad_dict)
    assert any("Field 'description' must be a non-empty string" in e for e in errs)

    # Invalid channel
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["channel"] = "fax"
    errs = validate_case(bad_dict)
    assert any("Invalid channel 'fax'" in e for e in errs)

    # Non-English in scored split dev fails
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["language"] = "es"
    bad_dict["split"] = "dev"
    bad_dict["scenarios"] = ["normal"]
    errs = validate_case(bad_dict)
    assert any("only allowed in 'pool' split" in e for e in errs)

    # Non-English in pool with out_of_scope_language note passes
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["language"] = "es"
    bad_dict["split"] = "pool"
    bad_dict["notes"] = "out_of_scope_language:es"
    bad_dict["scenarios"] = ["normal"]
    errs = validate_case(bad_dict)
    assert errs == []

    # pii_scrubbed is False
    bad_dict = sample_routing_cases[0].to_dict()
    bad_dict["pii_scrubbed"] = False
    errs = validate_case(bad_dict)
    assert any("Field 'pii_scrubbed' must be True" in e for e in errs)


def test_jsonl_read_write(tmp_path: Path, sample_routing_cases):
    """# REQ-REVAL-002: write_jsonl sorts by ticket_id and read_jsonl recovers cases."""
    target_file = tmp_path / "test_split.jsonl"
    write_jsonl(target_file, sample_routing_cases)

    # Check sort order on disk
    loaded = read_jsonl(target_file)
    assert len(loaded) == len(sample_routing_cases)
    ticket_ids = [c.ticket_id for c in loaded]
    assert ticket_ids == sorted(ticket_ids)

    # Ensure UTF-8 with newline endings
    raw_bytes = target_file.read_bytes()
    assert raw_bytes.endswith(b"\n")
