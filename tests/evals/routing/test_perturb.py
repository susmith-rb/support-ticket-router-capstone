"""Acceptance tests for perturbation and paraphrase suites.

# REQ-REVAL-005: Deterministic perturbation & paraphrase suites
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.routing.perturb import (
    MEANING_PRESERVING_TYPES,
    derive,
    export_paraphrase_packets,
    ingest_paraphrases,
    perturb_case,
    perturb_conflicting_info,
    perturb_emoji_slang,
    perturb_long_padding,
    perturb_prompt_injection,
    perturb_punctuation_strip,
    perturb_truncate,
    perturb_typos,
    perturb_whitespace_noise,
)
from evals.routing.schema import (
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
    validate_case,
)


def _make_dummy_case(
    description: str = "Where is my order #12345? Tracking stopped updating.",
    intent: str = "order_status",
    gt_source: str = "human_adjudicated",
    label_status: str = "verified",
    verified_fields: tuple[str, ...] = ("intent", "decision", "queue", "escalate"),
) -> RoutingCase:
    c_hash = compute_content_hash(subject=None, description=description)
    t_id = make_ticket_id("kaggle_twcs", c_hash)
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
            escalate=False,
            decision="auto_rag",
            queue="none",
            sentiment="neutral",
            urgency=1,
        ),
        difficulty="medium",
        scenarios=("normal",),
        style=Style(register="casual", length_bucket="short (<20 words)"),
        source="real",
        source_dataset="kaggle_twcs",
        source_ref="test_ref",
        ground_truth_source=gt_source,
        label_status=label_status,
        verified_fields=verified_fields,
        annotations=(),
        adjudication=None,
        group_id=f"grp-{t_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
        notes="dummy",
    )


def test_perturb_functions_pure():
    """# REQ-REVAL-005: Individual perturbation functions are pure and deterministic."""
    base = "Where is my package? The tracking has not updated."

    # Typos
    t1 = perturb_typos(base, seed=42)
    t2 = perturb_typos(base, seed=42)
    assert t1 == t2
    assert t1 != base

    # Case
    assert perturb_case(base, seed=42, mode="lower") == base.lower()
    assert perturb_case(base, seed=42, mode="upper") == base.upper()

    # Punctuation strip
    p_stripped = perturb_punctuation_strip(base, seed=42)
    assert "?" not in p_stripped
    assert "." not in p_stripped

    # Whitespace noise
    w_noisy = perturb_whitespace_noise(base, seed=42)
    assert "  " in w_noisy

    # Emoji slang
    e_slang = perturb_emoji_slang(base, seed=42)
    assert any(tok in e_slang for tok in ["plz", "thx", "asap", "🙏", "📦", "⚠️", "👀", "urgent!!"])

    # Truncate
    trunc = perturb_truncate(base, seed=42, drop_fraction=0.5)
    assert len(trunc.split()) < len(base.split())

    # Long padding
    padded = perturb_long_padding(base, seed=42)
    assert len(padded.split()) > 40

    # Prompt injection
    injected = perturb_prompt_injection(base, seed=42)
    assert any(phrase in injected for phrase in ["Ignore previous instructions", "SYSTEM", "OVERRIDE"])

    # Conflicting info
    contra = perturb_conflicting_info(base, seed=42)
    assert "cancel" in contra.lower()


def test_derive_meaning_preserving_inheritance():
    """# REQ-REVAL-005: Meaning-preserving perturbations inherit parent's verified fields and GT source."""
    parent = _make_dummy_case(
        gt_source="human_adjudicated",
        label_status="verified",
        verified_fields=("intent", "decision", "queue", "escalate"),
    )

    for ptype in MEANING_PRESERVING_TYPES:
        derived = derive(parent, ptype=ptype, seed=123)
        errs = validate_case(derived)
        assert not errs, f"Derived case for {ptype} invalid: {errs}"

        # Verified labels and status are inherited
        assert derived.ground_truth_source == "human_adjudicated"
        assert derived.label_status == "verified"
        assert derived.verified_fields == ("intent", "decision", "queue", "escalate")
        assert derived.expected == parent.expected

        # Provenance links
        assert derived.parent_ticket_id == parent.ticket_id
        assert derived.group_id == parent.group_id
        assert "perturbation" in derived.scenarios
        assert derived.perturbation is not None
        assert derived.perturbation.type == ptype


def test_derive_truncate_missing_info_rederived():
    """# REQ-REVAL-005: Truncate perturbation causes missing_info and re-derives expected labels as provisional."""
    parent = _make_dummy_case(description="Where is order [ORDER]?")
    derived = derive(parent, ptype="truncate", seed=42, drop_fraction=0.8)

    errs = validate_case(derived)
    assert not errs, f"Derived truncate invalid: {errs}"

    # Does NOT inherit verified ground truth
    assert derived.ground_truth_source == "rule_derived"
    assert derived.label_status == "provisional"
    assert derived.verified_fields == ()
    assert "missing_info" in derived.scenarios
    assert "perturbation" in derived.scenarios


def test_derive_conflicting_info_rederived():
    """# REQ-REVAL-005: Conflicting info perturbation re-derives expected labels per Rule 5."""
    parent = _make_dummy_case(description="Where is my shipment?")
    derived = derive(parent, ptype="conflicting_info", seed=42, contradiction="Please cancel this order immediately.")

    errs = validate_case(derived)
    assert not errs, f"Derived conflicting info invalid: {errs}"

    # Rule 5: most recent explicit customer statement (cancel)
    assert derived.expected.intent == "order_status"
    assert "conflicting" in derived.scenarios
    assert derived.ground_truth_source == "rule_derived"
    assert derived.label_status == "provisional"
    assert derived.verified_fields == ()


def test_paraphrase_export_and_ingest(tmp_path: Path):
    """# REQ-REVAL-005: Paraphrase packets export and ingest workflow."""
    parent = _make_dummy_case()
    pkt_path = tmp_path / "para_packets.jsonl"
    packets = export_paraphrase_packets([parent], seed=42, out_path=pkt_path)
    assert len(packets) == 1
    assert pkt_path.exists()

    # Simulate paraphrase responses
    resp_path = tmp_path / "para_responses.jsonl"
    resp_data = [
        {
            "parent_ticket_id": parent.ticket_id,
            "paraphrased_text": "Could you tell me the current status of order 12345? Tracking has not moved.",
            "register": "formal",
        }
    ]
    with resp_path.open("w", encoding="utf-8") as f:
        for item in resp_data:
            f.write(json.dumps(item) + "\n")

    ingested, report = ingest_paraphrases(resp_path, [parent])
    assert len(ingested) == 1
    assert report["rejected_count"] == 0

    p_case = ingested[0]
    assert p_case.parent_ticket_id == parent.ticket_id
    assert p_case.group_id == parent.group_id
    assert "paraphrase" in p_case.scenarios
    assert p_case.ground_truth_source == "synthetic_intended"
    assert p_case.label_status == "provisional"
