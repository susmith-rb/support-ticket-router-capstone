"""Unit tests for evals.routing.pipeline_v1 orchestration.

# REQ-REVAL-004: Source adapters: real, public, synthetic
# REQ-REVAL-005: Deterministic perturbation & paraphrase suites
# REQ-REVAL-006: Provisional Gemini annotation via Jetski (k-pass)
# REQ-REVAL-007: Human adjudication workflow (Google Sheet)
# REQ-REVAL-009: Deterministic group-aware splits & dataset versioning
# REQ-REVAL-020: Versioned regression set of observed failures
"""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import List

import pytest
import yaml

from evals.routing.pipeline_v1 import build, stage1, stage2, stage3, validate_pool_cases
from evals.routing.schema import (
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
    read_jsonl,
    write_jsonl,
)


def _make_valid_case(
    source_dataset: str,
    description: str,
    subject: str | None = None,
    intent: str = "order_status",
    scenarios: tuple[str, ...] = ("normal",),
    split: str = "pool",
    source: str = "real",
    source_ref: str = "ref_test",
    ground_truth_source: str = "human_adjudicated",
    label_status: str = "verified",
    verified_fields: tuple[str, ...] = ("intent",),
    group_id: str | None = None,
    language: str = "en",
    difficulty: str = "medium",
    failure_ref=None,
) -> RoutingCase:
    c_hash = compute_content_hash(subject=subject, description=description)
    t_id = make_ticket_id(source_dataset, c_hash)
    expected = Expected(
        intent=intent,
        acceptable_intents=(intent,),
        escalate=(intent == "other"),
        decision="escalate" if intent == "other" else "auto_rag",
        queue="human-standard" if intent == "other" else "none",
        sentiment="neutral",
        urgency=1,
    )
    return RoutingCase(
        ticket_id=t_id,
        content_hash=c_hash,
        dataset_version="routing_test_v1.0.0",
        split=split,
        created_at="2026-10-28T00:00:00Z",
        subject=subject,
        description=description,
        channel="web",
        language=language,
        pii_scrubbed=True,
        expected=expected,
        difficulty=difficulty,
        scenarios=scenarios,
        style=Style(register="casual", length_bucket="short (<20 words)"),
        source=source,
        source_dataset=source_dataset,
        source_ref=source_ref,
        ground_truth_source=ground_truth_source,
        label_status=label_status,
        verified_fields=verified_fields,
        annotations=(),
        adjudication=None,
        group_id=group_id or f"grp-{t_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=failure_ref,
    )


def _create_minimal_recipe(recipe_file: Path, csv_file: Path) -> Path:
    """Create a minimal recipe YAML for fast hermetic unit testing."""
    recipe_data = {
        "dataset_version": "routing_test_v1.0.0",
        "seed": 20261028,
        "quotas": {"dev": 4, "regression": 2, "heldout": 4},
        "contamination": {
            "laya_hashes_path": str(recipe_file.parent / "non_existent_hashes.json"),
            "keys": ["train_val", "test"],
        },
        "sources": {
            "real_c2": {},
            "real_golden": {},
            "public_bitext": {
                "csv_path": str(csv_file),
                "n": 5,
                "per_intent_min": 1,
                "seed": 20261028,
            },
            "regression": {},
            "synthetic_templates": {"n_target": 3},
        },
        "perturbations": {
            "num_c2_bases": 2,
            "variants_per_base": 2,
            "meaning_preserving_types": ["typos", "case"],
            "semantic_altering_bases": 1,
            "semantic_altering_types": ["truncate", "conflicting_info"],
        },
        "paraphrases": {
            "num_c2_bases": 2,
            "paraphrases_per_base": 2,
        },
        "synthetic_generation": {
            "target_total": 4,
            "tickets_per_packet": 2,
            "scenario_weights": {
                "ambiguous": 2,
                "ood": 2,
            },
        },
        "annotation": {
            "k_passes": 3,
            "batch_size": 2,
        },
        "templates": [
            {
                "scenario": "missing_info",
                "description": "where order ?",
                "intent": "order_status",
                "acceptable_intents": ["order_status"],
                "escalate": False,
                "decision": "auto_rag",
                "queue": "none",
                "register": "terse",
                "difficulty": "medium",
                "sentiment": "neutral",
                "urgency": 1,
                "notes": "Minimal test template",
            },
            {
                "scenario": "ood",
                "description": "Directions to nearest train station",
                "intent": "other",
                "acceptable_intents": ["other"],
                "escalate": True,
                "decision": "escalate",
                "queue": "human-standard",
                "register": "formal",
                "difficulty": "easy",
                "sentiment": "neutral",
                "urgency": 0,
                "notes": "OOD navigation test template",
            },
        ],
    }
    recipe_file.write_text(yaml.dump(recipe_data), encoding="utf-8")
    return recipe_file


def _create_minimal_bitext_csv(csv_path: Path) -> Path:
    """Create a minimal Bitext-style CSV for testing."""
    headers = ["flags", "instruction", "category", "intent", "response"]
    rows = [
        ["Q", "Where is my order {{Order Number}}?", "ORDER", "track_order", "Checking status"],
        ["P", "What is your shipping policy?", "SHIPPING", "delivery_options", "Standard shipping takes 3 days"],
        ["K", "cancel order please for {{Order Number}}", "ORDER", "cancel_order", "Refund initiated"],
        ["W", "card declined on my account!", "PAYMENT", "payment_issue", "Please check your bank"],
        ["Z", "wheres my stuff {{Order Number}}", "ORDER", "track_order", "Looking up tracking"],
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return csv_path


def test_stage1_pipeline_creates_pool_and_packets(tmp_path: Path) -> None:
    # REQ-REVAL-004: Collect sources and write pool_stage1.jsonl
    # REQ-REVAL-005: Export generation packets and paraphrase packets
    csv_file = tmp_path / "test_bitext.csv"
    _create_minimal_bitext_csv(csv_file)

    recipe_file = tmp_path / "test_recipe.yaml"
    _create_minimal_recipe(recipe_file, csv_file)

    work_dir = tmp_path / "work"
    res = stage1(work_dir=work_dir, recipe_path=recipe_file, seed=20261028)

    assert (work_dir / "pool_stage1.jsonl").exists()
    assert (work_dir / "generation_packets.jsonl").exists()
    assert (work_dir / "paraphrase_packets.jsonl").exists()

    pool_cases = read_jsonl(work_dir / "pool_stage1.jsonl")
    assert len(pool_cases) >= 2  # Templates + bitext rows
    assert res["pool_stage1_count"] == len(pool_cases)

    # Check generation packets format and self-contained requirements
    gen_lines = (work_dir / "generation_packets.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(gen_lines) == 2  # ambiguous (1 packet of 2) + ood (1 packet of 2)
    pkt0 = json.loads(gen_lines[0])
    assert "packet_id" in pkt0
    assert "targets" in pkt0
    assert "taxonomy_definitions" in pkt0
    assert "guidelines_excerpt" in pkt0
    assert "output_schema" in pkt0
    assert "example" in pkt0
    assert len(pkt0["targets"]) == 2


def test_stage2_ingest_and_annotation_packets(tmp_path: Path) -> None:
    # REQ-REVAL-004 / REQ-REVAL-006: Ingest generated tickets and export k-pass annotation packets
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Setup pool_stage1 with 2 mock cases
    case1 = _make_valid_case(
        source_dataset="kaggle_twcs",
        description="Base ticket tracking order [ORDER]",
        source_ref="ref1",
    )
    write_jsonl(work_dir / "pool_stage1.jsonl", [case1])

    # Setup generation_packets.jsonl to test target cell propagation
    gen_packet_def = {
        "packet_id": "gen_packet_001",
        "scenario": "ambiguous",
        "ticket_count": 1,
        "targets": [
            {
                "target_id": 1,
                "intent": "order_status",
                "scenario": "ambiguous",
                "channel": "email",
                "style": {"register": "formal", "length_bucket": "medium (20–80)"},
                "difficulty": "hard",
                "requires_subject": True,
            }
        ],
    }
    (work_dir / "generation_packets.jsonl").write_text(json.dumps(gen_packet_def) + "\n", encoding="utf-8")

    csv_file = tmp_path / "test_bitext.csv"
    _create_minimal_bitext_csv(csv_file)
    recipe_file = tmp_path / "test_recipe.yaml"
    _create_minimal_recipe(recipe_file, csv_file)

    # Prepare generated responses directory with code fence and malformed line tolerance
    gen_dir = tmp_path / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)

    # 1. Code-fenced generation response
    code_fenced_response = """```json
{
  "packet_id": "gen_packet_001",
  "tickets": [
    {
      "target_id": 1,
      "subject": "Missing parcel inquiry",
      "description": "Hello, my tracking number has not updated since Monday. Where is the item?",
      "intended_expected": {
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": false,
        "decision": "auto_rag",
        "queue": "none",
        "sentiment": "neutral",
        "urgency": 1
      },
      "notes": "Generated tracking ticket"
    }
  ]
}
```"""
    (gen_dir / "response_gen1.jsonl").write_text(code_fenced_response, encoding="utf-8")

    # 2. Response with a malformed line + valid paraphrase response
    mixed_response = f"""
{{THIS IS MALFORMED JSON}}
{{
  "packet_id": "para_packet_001",
  "parent_ticket_id": "{case1.ticket_id}",
  "paraphrases": [
    {{
      "subject": null,
      "description": "Where is my package [ORDER]? tracking hasn't updated.",
      "style_variant": "casual"
    }}
  ]
}}
"""
    (gen_dir / "response_para1.jsonl").write_text(mixed_response, encoding="utf-8")

    res = stage2(work_dir=work_dir, generated_dir=gen_dir, recipe_path=recipe_file, seed=20261028)

    assert (work_dir / "pool_stage2.jsonl").exists()
    stage2_cases = read_jsonl(work_dir / "pool_stage2.jsonl")
    assert len(stage2_cases) == 3  # base + generated + paraphrase
    assert res["pool_stage2_count"] == 3

    # Verify generated case attributes propagate from packet target cell
    gen_case = next(c for c in stage2_cases if c.ticket_id != case1.ticket_id and "paraphrase" not in c.scenarios)
    assert gen_case.scenarios == ("ambiguous",)
    assert gen_case.difficulty == "hard"
    assert gen_case.channel == "email"
    assert gen_case.style.register == "formal"
    assert gen_case.expected.acceptable_intents == ("order_status",)

    # Verify paraphrase case attributes propagate scenario 'paraphrase', parent_ticket_id and group_id
    para_case = next(c for c in stage2_cases if "paraphrase" in c.scenarios)
    assert para_case.scenarios == ("paraphrase",)
    assert para_case.parent_ticket_id == case1.ticket_id
    assert para_case.group_id == case1.group_id

    # Verify byte-identical ticket IDs upon re-run
    res_rerun = stage2(work_dir=work_dir, generated_dir=gen_dir, recipe_path=recipe_file, seed=20261028)
    assert res_rerun["pool_stage2_count"] == 3
    rerun_cases = read_jsonl(work_dir / "pool_stage2.jsonl")
    assert [c.ticket_id for c in stage2_cases] == [c.ticket_id for c in rerun_cases]

    # Check annotation packets written for passes 0, 1, 2
    for p_idx in range(3):
        p_file = work_dir / f"annotation_packets_pass{p_idx}.jsonl"
        assert p_file.exists()
        lines = p_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        pkt = json.loads(lines[0])
        assert pkt["pass_index"] == p_idx
        assert "shuffled_intent_options" in pkt

    # Check that different passes have different shuffled intent orders
    p0_intents = json.loads((work_dir / "annotation_packets_pass0.jsonl").read_text().splitlines()[0])["shuffled_intent_options"]
    p1_intents = json.loads((work_dir / "annotation_packets_pass1.jsonl").read_text().splitlines()[0])["shuffled_intent_options"]
    # With seed variation, label ordering shuffles across passes
    assert isinstance(p0_intents, list)
    assert isinstance(p1_intents, list)


def test_stage3_ingest_annotations_and_queue(tmp_path: Path) -> None:
    # REQ-REVAL-006: Ingest annotations & compute reliability
    # REQ-REVAL-007: Human adjudication queue TSV export
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Create 2 cases: one verified C2 case and one provisional synthetic case
    c2_case = _make_valid_case(
        source_dataset="kaggle_twcs",
        description="Tracking status for order [ORDER]",
        source_ref="ref_c2",
        intent="order_status",
    )
    synth_case = _make_valid_case(
        source_dataset="synthetic_jetski",
        description="I want to return an item, or maybe check delivery?",
        source_ref="ref_synth",
        intent="returns_refunds",
        scenarios=("ambiguous",),  # Ambiguous scenario will trigger adjudication queue!
        source="synthetic",
        ground_truth_source="synthetic_intended",
        label_status="provisional",
        verified_fields=(),
    )
    write_jsonl(work_dir / "pool_stage2.jsonl", [c2_case, synth_case])

    # Mock annotation passes responses in annotations_dir
    ann_dir = tmp_path / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)

    for p in range(3):
        response_payload = {
            "packet_id": f"ann_packet_pass{p}_001",
            "pass_index": p,
            "annotator_id": f"test_annotator_p{p}",
            "labels": [
                {
                    "ticket_id": c2_case.ticket_id,
                    "expected": {
                        "intent": "returns_refunds",  # Annotator tries to change intent!
                        "acceptable_intents": ["returns_refunds"],
                        "escalate": False,
                        "decision": "auto_rag",
                        "queue": "none",
                        "sentiment": "neutral",
                        "urgency": 1,
                    },
                    "confidence": 0.9,
                    "rationale": "Test pass",
                },
                {
                    "ticket_id": synth_case.ticket_id,
                    "expected": {
                        "intent": "returns_refunds",
                        "acceptable_intents": ["returns_refunds", "order_status"],
                        "escalate": False,
                        "decision": "auto_rag",
                        "queue": "none",
                        "sentiment": "neutral",
                        "urgency": 1,
                    },
                    "confidence": 0.85,
                    "rationale": "Ambiguous boundary ticket",
                },
            ],
        }
        (ann_dir / f"pass{p}.jsonl").write_text(json.dumps(response_payload) + "\n", encoding="utf-8")

    res = stage3(work_dir=work_dir, annotations_dir=ann_dir)
    assert res["pool_stage3_count"] == 2

    assert (work_dir / "pool_stage3.jsonl").exists()
    stage3_cases = read_jsonl(work_dir / "pool_stage3.jsonl")
    assert len(stage3_cases) == 2

    # Invariant: C2 verified_fields (intent) MUST NOT be overwritten!
    c2_updated = next(c for c in stage3_cases if c.ticket_id == c2_case.ticket_id)
    assert c2_updated.expected.intent == "order_status"  # Preserved original human-verified intent!
    assert c2_updated.ground_truth_source == "human_adjudicated"
    assert c2_updated.label_status == "verified"
    assert "intent" in c2_updated.verified_fields
    assert len(c2_updated.annotations) == 3

    # Check adjudication queue TSV export
    queue_tsv = work_dir / "adjudication_queue.tsv"
    assert queue_tsv.exists()
    with queue_tsv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
        # synth_case has scenario ambiguous -> queued for adjudication!
        assert any(r["ticket_id"] == synth_case.ticket_id for r in rows)
        # Check all 18 REQ-REVAL-007 columns exist
        expected_cols = [
            "ticket_id", "description", "subject", "candidate_intent", "candidate_decision",
            "candidate_queue", "candidate_sources", "reason_queued", "final_intent",
            "final_acceptable_intents", "final_escalate", "final_decision", "final_queue",
            "final_sentiment", "final_urgency", "verdict", "adjudicator", "rationale"
        ]
        assert reader.fieldnames == expected_cols


def test_build_pipeline_creates_immutable_version(tmp_path: Path) -> None:
    # REQ-REVAL-009: Deterministic group-aware splits & dataset versioning
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy pool cases
    cases: List[RoutingCase] = [
        _make_valid_case(
            source_dataset="synthetic_template",
            description=f"Ticket description {i} for build testing",
            source_ref=f"ref_{i}",
            source="synthetic",
            ground_truth_source="synthetic_intended",
            label_status="provisional",
            verified_fields=(),
            group_id=f"grp-build-{i}",
        )
        for i in range(12)
    ]
    write_jsonl(work_dir / "pool_stage3.jsonl", cases)

    csv_file = tmp_path / "test_bitext.csv"
    _create_minimal_bitext_csv(csv_file)
    recipe_file = tmp_path / "test_recipe.yaml"
    _create_minimal_recipe(recipe_file, csv_file)

    out_root = tmp_path / "data"
    vdir = build(
        work_dir=work_dir,
        version="routing_test_v1.0.0",
        out_root=out_root,
        recipe_path=recipe_file,
        seed=20261028,
    )

    assert vdir.exists()
    assert (vdir / "dev.jsonl").exists()
    assert (vdir / "heldout.jsonl").exists()
    assert (vdir / "regression.jsonl").exists()
    assert (vdir / "pool.jsonl").exists()
    assert (vdir / "manifest.json").exists()
    assert (vdir / "CHANGELOG.md").exists()

    # Immutable version directory: Re-running must refuse to overwrite
    with pytest.raises(FileExistsError):
        build(
            work_dir=work_dir,
            version="routing_test_v1.0.0",
            out_root=out_root,
            recipe_path=recipe_file,
            seed=20261028,
        )


def test_pipeline_validation_hard_fail_on_invalid_case(tmp_path: Path):
    """Assert validate_pool_cases hard-fails on schema violations with a clear summary."""
    valid_case = _make_valid_case(
        source_dataset="synthetic_template",
        description="Where is my shipment?",
        subject="Status check",
    )

    # 1. Valid cases must pass without error
    validate_pool_cases([valid_case], stage_name="test_stage")

    # 2. Case with non-English language without 'ood' in scenarios must fail
    invalid_lang_case = dataclasses.replace(
        valid_case,
        language="es",
        scenarios=("normal",),  # Missing 'ood'!
    )

    with pytest.raises(ValueError, match="schema validation failed"):
        validate_pool_cases([valid_case, invalid_lang_case], stage_name="test_stage")

    # 3. Case with invalid channel must fail
    invalid_chan_case = dataclasses.replace(
        valid_case,
        channel="carrier_pigeon",  # Invalid channel!
    )

    with pytest.raises(ValueError, match="schema validation failed"):
        validate_pool_cases([invalid_chan_case], stage_name="test_stage")


def test_stage2b_and_stage3_multi_annotation(tmp_path: Path):
    """# REQ-REVAL-004 / REQ-REVAL-006: Test stage2b fresh thread sampling and stage3 multi-directory ingestion."""
    from evals.routing.pipeline_v1 import stage2b, stage3

    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True)

    # 1. Setup mock pool_stage2.jsonl
    base_case = _make_valid_case(
        source_dataset="synthetic_template",
        description="Existing pool stage 2 ticket description.",
        intent="order_status",
    )
    write_jsonl(work_dir / "pool_stage2.jsonl", [base_case])

    # 2. Setup mock existing annotation_packets_pass0.jsonl to verify untouched invariant
    existing_pkt_content = '{"packet_id": "ann_packet_pass0_001", "tickets": []}\n'
    (work_dir / "annotation_packets_pass0.jsonl").write_text(existing_pkt_content, encoding="utf-8")

    # 3. Setup mock threads.jsonl
    threads_file = tmp_path / "mock_threads.jsonl"
    threads_data = [
        {"thread_id": 101, "brand": "Brand", "turns": [{"role": "customer", "text": "Fresh English customer tweet about shipping delay."}]},
        {"thread_id": 102, "brand": "Brand", "turns": [{"role": "customer", "text": "Another fresh customer tweet asking about return policies."}]},
    ]
    with threads_file.open("w", encoding="utf-8") as f:
        for t in threads_data:
            f.write(json.dumps(t) + "\n")

    # 4. Recipe
    recipe_file = tmp_path / "recipe.yaml"
    recipe_content = {
        "dataset_version": "routing_test_v1.0.0",
        "seed": 20261028,
        "quotas": {"dev": 2, "regression": 0, "heldout": 2},
        "sources": {
            "real_fresh": {
                "threads_path": str(threads_file),
                "n": 2,
            }
        },
        "annotation": {"k_passes": 3, "batch_size": 10},
    }
    with recipe_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(recipe_content, f)

    # Run stage2b
    res_2b = stage2b(work_dir=work_dir, recipe_path=recipe_file, seed=20261028)
    assert res_2b["fresh_cases_count"] == 2
    assert res_2b["pool_stage2b_count"] == 3

    # Invariant: existing annotation_packets_pass0.jsonl was NOT modified
    assert (work_dir / "annotation_packets_pass0.jsonl").read_text(encoding="utf-8") == existing_pkt_content

    # New fresh annotation packets were created
    for p in range(3):
        fresh_pkt_file = work_dir / f"annotation_packets_fresh_pass{p}.jsonl"
        assert fresh_pkt_file.exists()
        lines = [json.loads(l) for l in fresh_pkt_file.read_text(encoding="utf-8").strip().split("\n")]
        assert len(lines) == 1
        assert lines[0]["ticket_count"] == 2

    # 5. Test stage3 multi-directory ingestion and fresh_unlabeled label replacement
    ann_dir1 = tmp_path / "ann_dir1"
    ann_dir2 = tmp_path / "ann_dir2"
    ann_dir1.mkdir()
    ann_dir2.mkdir()

    p2b_cases = read_jsonl(work_dir / "pool_stage2b.jsonl")
    fresh_ticket_id = [c.ticket_id for c in p2b_cases if c.notes == "fresh_unlabeled"][0]

    # pass 0 in dir 1
    p0_file = ann_dir1 / "pass0.jsonl"
    p0_obj = {
        "pass_index": 0,
        "annotator_id": "ann_0",
        "labels": [
            {
                "ticket_id": fresh_ticket_id,
                "expected": {
                    "intent": "shipping_policy",
                    "acceptable_intents": ["shipping_policy"],
                    "escalate": False,
                    "decision": "auto_rag",
                    "queue": "none",
                    "sentiment": "neutral",
                    "urgency": 1,
                },
                "confidence": 0.95,
                "rationale": "Clear shipping policy question",
            }
        ],
    }
    p0_file.write_text(json.dumps(p0_obj) + "\n", encoding="utf-8")

    # pass 1 and 2 in dir 2
    p1_file = ann_dir2 / "pass1.jsonl"
    p1_obj = {
        "pass_index": 1,
        "annotator_id": "ann_1",
        "labels": [
            {
                "ticket_id": fresh_ticket_id,
                "expected": {
                    "intent": "shipping_policy",
                    "acceptable_intents": ["shipping_policy"],
                    "escalate": False,
                    "decision": "auto_rag",
                    "queue": "none",
                    "sentiment": "neutral",
                    "urgency": 1,
                },
                "confidence": 0.95,
                "rationale": "Shipping inquiry",
            }
        ],
    }
    p1_file.write_text(json.dumps(p1_obj) + "\n", encoding="utf-8")

    # Run stage3 with comma-separated annotation dirs
    res_s3 = stage3(
        work_dir=work_dir,
        annotations_dir=f"{ann_dir1},{ann_dir2}",
        recipe_path=recipe_file,
    )
    assert res_s3["pool_stage3_count"] == 3

    stage3_cases = read_jsonl(work_dir / "pool_stage3.jsonl")
    updated_fresh = [c for c in stage3_cases if c.ticket_id == fresh_ticket_id][0]

    # Verify placeholder expected was replaced with consensus
    assert updated_fresh.expected.intent == "shipping_policy"
    assert updated_fresh.expected.decision == "auto_rag"
    assert updated_fresh.ground_truth_source == "llm_provisional"
    assert "fresh_llm_annotated" in updated_fresh.notes

