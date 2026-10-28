"""Unit tests for deterministic group-aware splits, manifest building, and dataset versioning.

# REQ-REVAL-009: Deterministic group-aware splits & dataset versioning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.routing.manifest import build_manifest, write_version_dir
from evals.routing.split import assign


def test_assign_pure_function_byte_identical(sample_routing_cases):
    """# REQ-REVAL-009: assign produces identical split allocations across multiple runs."""
    run1 = assign(sample_routing_cases, seed=20261028, quotas={"dev": 10, "heldout": 10, "regression": 5})
    run2 = assign(sample_routing_cases, seed=20261028, quotas={"dev": 10, "heldout": 10, "regression": 5})

    assert len(run1) == len(run2)
    for c1, c2 in zip(run1, run2):
        assert c1.ticket_id == c2.ticket_id
        assert c1.split == c2.split
        assert c1.group_id == c2.group_id


def test_assign_group_aware_isolation(sample_routing_cases):
    """# REQ-REVAL-009: Cases in the same group_id are never broken across splits."""
    quotas = {"dev": 5, "heldout": 5, "regression": 2}
    assigned = assign(sample_routing_cases, seed=20261028, quotas=quotas)

    group_splits: dict[str, set[str]] = {}
    for c in assigned:
        group_splits.setdefault(c.group_id, set()).add(c.split)

    for grp_id, splits in group_splits.items():
        assert len(splits) == 1, f"Group {grp_id} was split across multiple splits: {splits}"


def test_assign_regression_curated_preserved(sample_routing_cases):
    """# REQ-REVAL-009: Regression cases are preserved in regression split and not resampled."""
    assigned = assign(sample_routing_cases, seed=20261028, quotas={"dev": 10, "heldout": 10, "regression": 10})
    reg_cases = [c for c in assigned if c.split == "regression"]
    assert len(reg_cases) >= 2
    for rc in reg_cases:
        assert rc.failure_ref is not None or "observed_failure" in rc.scenarios


def test_assign_quota_enforcement_and_pool_overflow(sample_routing_cases):
    """# REQ-REVAL-009: Quotas are respected and overflow cases are routed to pool."""
    quotas = {"dev": 4, "heldout": 4, "regression": 2}
    assigned = assign(sample_routing_cases, seed=20261028, quotas=quotas)

    dev_count = sum(1 for c in assigned if c.split == "dev")
    heldout_count = sum(1 for c in assigned if c.split == "heldout")
    pool_count = sum(1 for c in assigned if c.split == "pool")

    assert dev_count <= 4
    assert heldout_count <= 4
    assert pool_count > 0, "Excess cases must be routed to pool"


def test_manifest_building_and_immutable_version_dir(tmp_path: Path, sample_routing_cases):
    """# REQ-REVAL-009: build_manifest and write_version_dir enforce immutability and complete metadata."""
    assigned = assign(sample_routing_cases, seed=20261028, quotas={"dev": 10, "heldout": 10, "regression": 2})
    splits = {
        "dev": [c for c in assigned if c.split == "dev"],
        "heldout": [c for c in assigned if c.split == "heldout"],
        "regression": [c for c in assigned if c.split == "regression"],
        "pool": [c for c in assigned if c.split == "pool"],
    }

    manifest_data = build_manifest(
        dataset_version="routing_v1.0.0",
        seed=20261028,
        splits=splits,
        coverage_report={"coverage_score": 1.0},
        dedup_report={"exact_duplicates_dropped": 0},
    )

    version_dir = tmp_path / "routing_v1.0.0"
    out_dir = write_version_dir(
        version_dir,
        splits=splits,
        manifest_data=manifest_data,
        changelog_entry="# Release routing_v1.0.0\n\n- Baseline release.",
    )

    assert out_dir.exists()
    assert (out_dir / "dev.jsonl").exists()
    assert (out_dir / "regression.jsonl").exists()
    assert (out_dir / "heldout.jsonl").exists()
    assert (out_dir / "pool.jsonl").exists()
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "CHANGELOG.md").exists()

    # Verify manifest content
    written_manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert written_manifest["dataset_version"] == "routing_v1.0.0"
    assert "dev.jsonl" in written_manifest["output_sha256s"]
    assert written_manifest["split_counts"]["dev"]["total"] == len(splits["dev"])

    # Verify immutability: attempting to write again raises FileExistsError
    with pytest.raises(FileExistsError, match="already exists"):
        write_version_dir(version_dir, splits=splits, manifest_data=manifest_data)


def test_assign_source_aware_split_mix():
    """# REQ-REVAL-004 / REQ-REVAL-009: assign with split_mix respects source quotas and isolates contamination."""
    from evals.routing.schema import Expected, RoutingCase, Style, compute_content_hash

    cases: list[RoutingCase] = []
    train_hashes = {"contam_hash_001", "contam_hash_002"}

    def _make(tid, src, src_ds, sref="", notes="", is_contam=False, grp=None, split="pool", pert=None, parent=None):
        chash = "contam_hash_001" if is_contam else compute_content_hash(None, f"text_{tid}")
        return RoutingCase(
            ticket_id=tid,
            content_hash=chash,
            dataset_version="v1",
            split=split,
            created_at="2026-10-28T00:00:00Z",
            subject=None,
            description=f"text_{tid}",
            channel="chat",
            language="en",
            pii_scrubbed=True,
            expected=Expected("order_status", ("order_status",), False, "auto_rag", "none", "neutral", 1),
            difficulty="medium",
            scenarios=("normal",),
            style=Style("casual", "short (<20 words)"),
            source=src,
            source_dataset=src_ds,
            source_ref=sref,
            ground_truth_source="rule_derived",
            label_status="provisional",
            verified_fields=(),
            annotations=(),
            adjudication=None,
            group_id=grp or f"grp-{tid}",
            parent_ticket_id=parent,
            perturbation=pert,
            failure_ref=None,
            notes=notes,
        )

    # 4 real_fresh
    for i in range(4):
        cases.append(_make(f"rf_{i}", "real", "kaggle_twcs", sref=f"twcs_thread_{i}", notes="fresh_unlabeled"))
    # 4 bitext
    for i in range(4):
        cases.append(_make(f"bx_{i}", "public", "bitext_customer_support"))
    # 4 synthetic_template
    for i in range(4):
        cases.append(_make(f"tmpl_{i}", "synthetic", "synthetic_template"))
    # 4 synthetic_jetski
    for i in range(4):
        cases.append(_make(f"gen_{i}", "synthetic", "synthetic_jetski"))
    # 2 contaminated C2
    cases.append(_make("c2_1", "real", "kaggle_twcs", sref="retrieval_c2_1", notes="c2_qid:1", is_contam=True))
    cases.append(_make("c2_2", "real", "kaggle_twcs", sref="retrieval_c2_2", notes="c2_qid:2", is_contam=True))

    split_mix = {
        "dev": {
            "real_fresh": 2,
            "bitext": 2,
            "synthetic_template": 2,
            "synthetic_jetski": 2,
            "contaminated_c2": 2,
        },
        "heldout": {
            "real_fresh": 2,
            "bitext": 2,
            "synthetic_template": 2,
            "synthetic_jetski": 2,
        },
    }

    assigned = assign(
        cases,
        seed=20261028,
        quotas={"dev": 10, "heldout": 8, "regression": 0},
        split_mix=split_mix,
        train_hashes=train_hashes,
    )

    dev_cases = [c for c in assigned if c.split == "dev"]
    heldout_cases = [c for c in assigned if c.split == "heldout"]

    assert len(heldout_cases) == 8
    # Heldout MUST have zero contaminated cases
    for c in heldout_cases:
        assert c.content_hash not in train_hashes
        assert "laya_train_collision" not in (c.notes or "")
        assert not c.source_ref.startswith("retrieval_c2")

    # Dev contains the contaminated C2 cases
    dev_c2 = [c for c in dev_cases if c.source_ref.startswith("retrieval_c2")]
    assert len(dev_c2) == 2

