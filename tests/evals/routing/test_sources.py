"""Acceptance tests for routing data source adapters.

# REQ-REVAL-004: Source adapters: real, public, synthetic
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.routing.schema import validate_case
from evals.routing.sources.public_bitext import (
    load_mapping,
    map_flags_to_register,
    render_placeholders,
)
from evals.routing.sources.public_bitext import (
    sample as sample_bitext,
)
from evals.routing.sources.real_twcs import (
    contamination_guard,
    scrub_text,
)
from evals.routing.sources.synthetic import (
    default_cells,
    export_generation_packets,
    generate_templates,
    ingest_generated,
)
from ticket_router.config.taxonomy import load_taxonomy, validate_expected

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FIXTURE_CSV = Path(__file__).resolve().parent / "fixtures" / "bitext_sample.csv"
_GUIDELINES_PATH = _REPO_ROOT / "evals" / "routing" / "GUIDELINES.md"


def test_bitext_mapping_all_27_intents_valid():
    """# REQ-REVAL-004: All 27 Bitext customer support intents must be mapped and validate."""
    mapping = load_mapping()
    assert len(mapping) == 27, f"Expected 27 mapped intents, got {len(mapping)}"

    tax = load_taxonomy()
    for b_intent, data in mapping.items():
        assert "intent" in data
        assert data["intent"] in tax.intents
        assert "decision" in data
        assert "queue" in data
        assert "rationale" in data and len(data["rationale"]) > 0

        exp_dict = {
            "intent": data["intent"],
            "acceptable_intents": data.get("acceptable_intents", [data["intent"]]),
            "escalate": (data["decision"] == "escalate"),
            "decision": data["decision"],
            "queue": data["queue"],
            "sentiment": "neutral",
            "urgency": 1,
        }
        val_errors = validate_expected(exp_dict)
        assert not val_errors, f"Validation failed for Bitext intent '{b_intent}': {val_errors}"


def test_guidelines_contains_taxonomy_definitions():
    """# REQ-REVAL-004: GUIDELINES.md must include all taxonomy intents, sentiments, and queues."""
    assert _GUIDELINES_PATH.exists(), f"Missing GUIDELINES.md at {_GUIDELINES_PATH}"
    text = _GUIDELINES_PATH.read_text(encoding="utf-8")

    tax = load_taxonomy()
    for intent in tax.intents:
        assert intent in text, f"Intent '{intent}' missing from GUIDELINES.md"

    for sentiment in tax.sentiments:
        assert sentiment in text, f"Sentiment '{sentiment}' missing from GUIDELINES.md"

    for queue in tax.queues:
        assert queue in text, f"Queue '{queue}' missing from GUIDELINES.md"


def test_bitext_placeholder_rendering():
    """# REQ-REVAL-004: Placeholder rendering replaces entities with natural phrases or tokens."""
    sample_text = "I need {{Order Number}} status and help with {{Invoice Number}} in {{Delivery City}}."
    rendered = render_placeholders(sample_text)
    assert "[ORDER]" in rendered
    assert "[INVOICE]" in rendered
    assert "Seattle" in rendered
    assert "{{" not in rendered


def test_bitext_flag_register_mapping():
    """# REQ-REVAL-004: Bitext flags map to canonical registers."""
    assert map_flags_to_register("Q") == "casual"
    assert map_flags_to_register("K") == "terse"
    assert map_flags_to_register("P") == "formal"
    assert map_flags_to_register("W") == "emotional"
    assert map_flags_to_register("B") == "casual"


def test_bitext_sampling_deterministic(tmp_path: Path):
    """# REQ-REVAL-004: Bitext sampling is deterministic and schema-compliant."""
    cases_1 = sample_bitext(_FIXTURE_CSV, n=15, seed=42, per_intent_min=1)
    cases_2 = sample_bitext(_FIXTURE_CSV, n=15, seed=42, per_intent_min=1)

    assert len(cases_1) == len(cases_2)
    assert [c.ticket_id for c in cases_1] == [c.ticket_id for c in cases_2]

    for c in cases_1:
        errors = validate_case(c)
        assert not errors, f"Case {c.ticket_id} invalid: {errors}"
        assert c.source == "public"
        assert c.source_dataset == "bitext_cs"
        assert c.ground_truth_source == "mapped_public"
        assert c.label_status == "provisional"
        assert c.channel == "chat"
        assert c.pii_scrubbed is True


def test_real_twcs_pii_scrubbing():
    """# REQ-REVAL-004: Production PII scrubber redacts sensitive patterns while preserving orders."""
    # 1. Order number format 3-7-7 should not be wiped as card
    text_order = "My order number is 112-1234567-1234567 please check where it is."
    scrubbed_order = scrub_text(text_order)
    assert "[ORDER]" in scrubbed_order
    assert "[CARD]" not in scrubbed_order

    # 2. Real card (valid Luhn) gets scrubbed
    # 4532 0150 1234 5671 is valid Luhn Visa
    text_card = "Refund my card 4532 0150 1234 5671 immediately."
    scrubbed_card = scrub_text(text_card)
    assert "[CARD]" in scrubbed_card

    # 3. Email, phone, Twitter mention, signature
    text_mix = "Hello @12345 email me at test@example.com or call +15551234567. ^AB"
    scrubbed_mix = scrub_text(text_mix)
    assert "@user" in scrubbed_mix
    assert "[EMAIL]" in scrubbed_mix
    assert "[PHONE]" in scrubbed_mix
    assert "^AB" not in scrubbed_mix


def test_real_twcs_contamination_guard():
    """# REQ-REVAL-004: Contamination guard identifies colliding cases against Laya training hashes."""
    cases = generate_templates(seed=123, n_per_cell=1)
    target_case = cases[0]
    train_hashes = {target_case.content_hash}

    guarded_cases, report = contamination_guard(cases, train_hashes)
    assert report["collisions_count"] == 1
    assert target_case.ticket_id in report["colliding_ticket_ids"]

    collided = next(c for c in guarded_cases if c.ticket_id == target_case.ticket_id)
    assert "laya_train_collision" in collided.notes


def test_synthetic_template_generator():
    """# REQ-REVAL-004: Synthetic template generator creates valid structural scenarios."""
    cases = generate_templates(seed=20261028, n_per_cell=2)
    assert len(cases) > 0

    scenarios_covered = {c.scenarios[0] for c in cases}
    assert "missing_info" in scenarios_covered
    assert "conflicting" in scenarios_covered
    assert "rare" in scenarios_covered
    assert "long" in scenarios_covered
    assert "ood" in scenarios_covered

    for c in cases:
        errs = validate_case(c)
        assert not errs, f"Case {c.ticket_id} invalid: {errs}"
        assert c.source == "synthetic"
        assert c.source_dataset == "synthetic_template"
        assert c.ground_truth_source == "synthetic_intended"
        assert c.label_status == "provisional"


def test_synthetic_export_and_ingest(tmp_path: Path):
    """# REQ-REVAL-004: Generation packets export and ingest workflow."""
    cells = default_cells(target_total=10)
    pkt_file = tmp_path / "packets.jsonl"
    packets = export_generation_packets(cells[:5], seed=123, out_path=pkt_file)
    assert len(packets) == 5
    assert pkt_file.exists()

    # Simulate generated output response
    gen_file = tmp_path / "generated.jsonl"
    generated_payload = [
        {
            "description": "Where is my package? The tracking has stopped moving.",
            "intended_expected": {
                "intent": "order_status",
                "acceptable_intents": ["order_status"],
                "escalate": False,
                "decision": "auto_rag",
                "queue": "none",
                "sentiment": "neutral",
                "urgency": 1,
            },
            "cell_info": {"scenario": "normal", "register": "casual", "difficulty": "easy"},
            "notes": "Generated sample",
        }
    ]
    with gen_file.open("w", encoding="utf-8") as f:
        for item in generated_payload:
            f.write(json.dumps(item) + "\n")

    ingested, report = ingest_generated(gen_file)
    assert len(ingested) == 1
    assert report["rejected_count"] == 0
    assert ingested[0].expected.intent == "order_status"
    assert ingested[0].ground_truth_source == "synthetic_intended"


def test_build_version_e2e(tmp_path: Path):
    """# REQ-REVAL-004: build_version end-to-end creates immutable version dir and splits."""
    from evals.routing.build import build_version

    inputs_cfg = {
        "bitext": {
            "csv_path": str(_FIXTURE_CSV),
            "n": 20,
            "per_intent_min": 1,
        },
        "synthetic_templates": {"n_per_cell": 1},
        "regression": {},
    }

    out_root = tmp_path / "data"
    version_dir = build_version(
        version="routing_test_v1.0.0",
        inputs_cfg=inputs_cfg,
        seed=20261028,
        out_root=out_root,
        quotas={"dev": 10, "heldout": 10, "regression": 10},
    )

    assert version_dir.exists()
    assert (version_dir / "dev.jsonl").exists()
    assert (version_dir / "heldout.jsonl").exists()
    assert (version_dir / "regression.jsonl").exists()
    assert (version_dir / "pool.jsonl").exists()
    assert (version_dir / "manifest.json").exists()
    assert (version_dir / "CHANGELOG.md").exists()

    # Verify manifest content
    with (version_dir / "manifest.json").open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["dataset_version"] == "routing_test_v1.0.0"
    assert "split_counts" in manifest

    # Second build must fail due to immutability
    with pytest.raises(FileExistsError):
        build_version(
            version="routing_test_v1.0.0",
            inputs_cfg=inputs_cfg,
            seed=20261028,
            out_root=out_root,
        )


def test_cli_data_subcommands(tmp_path: Path):
    """# REQ-REVAL-004: cli_data.main executes subcommands properly."""
    from evals.routing.cli_data import main as cli_main

    # 1. sources
    out_sources = tmp_path / "pool.jsonl"
    rc = cli_main(["sources", "--bitext-csv", str(_FIXTURE_CSV), "--n", "5", "--out", str(out_sources)])
    assert rc == 0
    assert out_sources.exists()

    # 2. export-annotations
    out_ann = tmp_path / "ann_packets.jsonl"
    rc = cli_main(["export-annotations", "--cases", str(out_sources), "--k", "2", "--out", str(out_ann)])
    assert rc == 0
    assert out_ann.exists()

    # 3. export-generation
    out_gen = tmp_path / "gen_packets.jsonl"
    rc = cli_main(["export-generation", "--out", str(out_gen)])
    assert rc == 0
    assert out_gen.exists()


def test_contamination_guard_heldout_reassignment():
    """# REQ-REVAL-004: Contamination guard forces colliding heldout cases to dev split."""
    from evals.routing.schema import Expected, RoutingCase, Style, compute_content_hash

    desc = "My flight was delayed and bag lost"
    c_hash = compute_content_hash(None, desc)
    train_hashes = {c_hash}

    c_heldout = RoutingCase(
        ticket_id="rt-heldout-001",
        content_hash=c_hash,
        dataset_version="v1.0",
        split="heldout",
        created_at="2026-10-28T12:00:00Z",
        subject=None,
        description=desc,
        channel="chat",
        language="en",
        pii_scrubbed=True,
        expected=Expected(
            intent="order_status",
            acceptable_intents=("order_status",),
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
        source_dataset="twcs",
        source_ref="tweet_1",
        ground_truth_source="mapped_public",
        label_status="provisional",
        verified_fields=(),
        annotations=(),
        adjudication=None,
        group_id="grp-1",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=None,
    )

    guarded, report = contamination_guard([c_heldout], train_hashes)
    assert report["collisions_count"] == 1
    assert "rt-heldout-001" in report["colliding_ticket_ids"]
    assert len(guarded) == 1
    # Colliding heldout case must be reassigned to dev
    assert guarded[0].split == "dev"
    assert "laya_train_collision" in (guarded[0].notes or "")


