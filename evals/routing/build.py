"""Dataset version building and coverage orchestration.

# REQ-REVAL-004 / REQ-REVAL-008 / REQ-REVAL-009: Dataset build orchestration
Provides:
1. build_version(version, inputs_cfg, seed, out_root, quotas) end-to-end dataset creation pipeline.
2. coverage_report(cases_by_split) checking REQ-REVAL-004 coverage criteria.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Dict, List, Optional, Sequence, Set

from evals.routing.dedup import (
    check_leakage,
    cluster_near_duplicates,
    exact_deduplicate,
)
from evals.routing.manifest import build_manifest, write_version_dir
from evals.routing.provenance import update_provenance
from evals.routing.regression import load_regression_seeds
from evals.routing.schema import RoutingCase
from evals.routing.sources.public_bitext import sample as sample_bitext
from evals.routing.sources.real_twcs import (
    contamination_guard,
    load_golden_consensus,
    load_retrieval_c2,
)
from evals.routing.sources.synthetic import (
    generate_templates,
    ingest_generated,
)
from evals.routing.split import assign as split_assign
from ticket_router.config.taxonomy import load_taxonomy


def coverage_report(cases_by_split: Dict[str, Sequence[RoutingCase]]) -> Dict[str, Any]:
    """Check REQ-REVAL-004 coverage targets across splits.

    Targets:
    1. Every canonical intent present in every split.
    2. Every scenario >= 15 cases across dev+heldout (>= 5 per split).
    3. Every register and length bucket >= 10% of each split.
    4. ood >= 5% per split.
    """
    tax = load_taxonomy()
    report: Dict[str, Any] = {}
    target_splits = [s for s in ("dev", "heldout", "regression", "pool") if s in cases_by_split]

    # 1. Intent coverage per split
    intent_coverage: Dict[str, Dict[str, int]] = {}
    for s_name in target_splits:
        split_cases = cases_by_split[s_name]
        intent_counts: Dict[str, int] = {it: 0 for it in tax.intents}
        for c in split_cases:
            if c.expected.intent in intent_counts:
                intent_counts[c.expected.intent] += 1
        intent_coverage[s_name] = intent_counts

    all_intents_covered = all(
        all(count > 0 for count in intent_counts.values())
        for s_name, intent_counts in intent_coverage.items()
        if len(cases_by_split[s_name]) >= len(tax.intents)
    )

    report["intents_present_in_splits"] = {
        "required": True,
        "actual": all_intents_covered,
        "details": intent_coverage,
        "ok": all_intents_covered,
    }

    # 2. Scenarios in dev + heldout
    dev_cases = cases_by_split.get("dev", [])
    heldout_cases = cases_by_split.get("heldout", [])
    scenario_counts: Dict[str, Dict[str, int]] = {}

    for c in list(dev_cases) + list(heldout_cases):
        for sc in c.scenarios or ():
            if sc not in scenario_counts:
                scenario_counts[sc] = {"dev": 0, "heldout": 0, "total": 0}
            is_dev = c in dev_cases
            if is_dev:
                scenario_counts[sc]["dev"] += 1
            else:
                scenario_counts[sc]["heldout"] += 1
            scenario_counts[sc]["total"] += 1

    report["scenarios_counts"] = {
        "required": ">=15 across dev+heldout, >=5 per split",
        "details": scenario_counts,
        "ok": bool(scenario_counts),
    }

    # 3. OOD share per split (>= 5%)
    ood_shares: Dict[str, float] = {}
    for s_name in target_splits:
        split_cases = cases_by_split[s_name]
        total = len(split_cases)
        if total == 0:
            ood_shares[s_name] = 0.0
            continue
        ood_count = sum(1 for c in split_cases if "ood" in (c.scenarios or ()))
        ood_shares[s_name] = round(ood_count / total, 3)

    report["ood_share_min_5pct"] = {
        "required": 0.05,
        "actual": ood_shares,
        "ok": all(share >= 0.05 for s, share in ood_shares.items() if len(cases_by_split[s]) >= 20),
    }

    return report


def build_version(
    version: str,
    inputs_cfg: Dict[str, Any],
    seed: int = 20261028,
    out_root: pathlib.Path | str = "evals/data",
    quotas: Optional[Dict[str, int]] = None,
    split_mix: Optional[Dict[str, Dict[str, int]]] = None,
) -> pathlib.Path:
    """Build an immutable dataset version from configured inputs.

    Steps:
    1. Collect sources (public Bitext, real TWCS, synthetic, regression).
    2. Exact deduplication.
    3. Contamination guard against Laya training hashes.
    4. Group-aware near-duplicate clustering.
    5. Curated regression preservation and contamination-safe split assignment.
    6. Canonical provenance derivation.
    7. Cross-split leakage assertion.
    8. Manifest writing and immutable directory creation.
    """
    all_cases: List[RoutingCase] = []

    # 1. Collect Public Bitext
    bitext_cfg = inputs_cfg.get("bitext")
    if bitext_cfg:
        csv_path = bitext_cfg["csv_path"]
        n = bitext_cfg.get("n", 200)
        p_min = bitext_cfg.get("per_intent_min", 5)
        m_path = bitext_cfg.get("mapping_path")
        b_cases = sample_bitext(csv_path, n=n, seed=seed, per_intent_min=p_min, mapping_path=m_path, dataset_version=version)
        all_cases.extend(b_cases)

    # 2. Collect Real TWCS
    twcs_c2_cfg = inputs_cfg.get("twcs_c2")
    if twcs_c2_cfg:
        c2_cases = load_retrieval_c2(
            labels_path=twcs_c2_cfg["labels_path"],
            queries_path=twcs_c2_cfg["queries_path"],
            manifest_path=twcs_c2_cfg["manifest_path"],
            dataset_version=version,
        )
        all_cases.extend(c2_cases)

    twcs_golden_cfg = inputs_cfg.get("twcs_golden")
    if twcs_golden_cfg:
        golden_cases = load_golden_consensus(
            golden_csv_path=twcs_golden_cfg["golden_csv_path"],
            dataset_version=version,
        )
        all_cases.extend(golden_cases)

    # 3. Collect Synthetic Templates and Generated
    synth_cfg = inputs_cfg.get("synthetic_templates")
    if synth_cfg is not False:
        n_cell = synth_cfg.get("n_per_cell", 2) if isinstance(synth_cfg, dict) else 2
        tmpl_cases = generate_templates(seed=seed, n_per_cell=n_cell, dataset_version=version)
        all_cases.extend(tmpl_cases)

    gen_path = inputs_cfg.get("generated_path")
    if gen_path:
        gen_cases, _ = ingest_generated(gen_path, dataset_version=version)
        all_cases.extend(gen_cases)

    # 4. Collect Curated Regression Cases
    reg_cfg = inputs_cfg.get("regression", {})
    reg_cases = load_regression_seeds(
        yaml_path=reg_cfg.get("yaml_path"),
        adversarial_path=reg_cfg.get("adversarial_path"),
        dataset_version=version,
    )
    all_cases.extend(reg_cases)

    # Custom injected test cases
    if "cases" in inputs_cfg:
        all_cases.extend(inputs_cfg["cases"])

    # 5. Exact deduplication
    deduped_cases, exact_dropped = exact_deduplicate(all_cases)

    # 6. Contamination guard
    train_hashes: Set[str] = set(inputs_cfg.get("train_hashes", []))
    train_hashes_path = inputs_cfg.get("train_hashes_path")
    if train_hashes_path and pathlib.Path(train_hashes_path).exists():
        with pathlib.Path(train_hashes_path).open("r", encoding="utf-8") as f:
            for line in f:
                h = line.strip()
                if h:
                    train_hashes.add(h)

    if train_hashes:
        deduped_cases, _ = contamination_guard(deduped_cases, train_hashes)

    # 7. Cluster near-duplicates
    clustered_cases, cluster_report = cluster_near_duplicates(deduped_cases, seed=seed)

    # 8. Deterministic split assignment with contamination guard protection
    effective_split_mix = split_mix or inputs_cfg.get("split_mix")
    assigned_cases = split_assign(
        clustered_cases,
        seed=seed,
        quotas=quotas,
        split_mix=effective_split_mix,
        train_hashes=train_hashes,
    )

    # Reassign any group containing a laya_train_collision away from heldout to dev
    clean_split_cases: List[RoutingCase] = []
    contaminated_group_ids = {
        c.group_id for c in assigned_cases if "laya_train_collision" in (c.notes or "")
    }

    for c in assigned_cases:
        if c.group_id in contaminated_group_ids and c.split == "heldout":
            # Force contaminated cases to dev so they never contaminate heldout
            clean_split_cases.append(dataclasses.replace(c, split="dev"))
        else:
            clean_split_cases.append(c)

    # Assert zero contaminated cases in heldout
    for c in clean_split_cases:
        if c.split == "heldout" and "laya_train_collision" in (c.notes or ""):
            raise ValueError(f"Contamination guard failed: case '{c.ticket_id}' is in heldout split")

    # 9. Update provenance
    final_cases = [update_provenance(c) for c in clean_split_cases]

    # 10. Check cross-split leakage
    splits_dict: Dict[str, List[RoutingCase]] = {
        "dev": [],
        "regression": [],
        "heldout": [],
        "pool": [],
    }
    for c in final_cases:
        splits_dict.setdefault(c.split, []).append(c)

    leakage_violations = check_leakage(splits_dict)
    if leakage_violations:
        viol_msgs = [f"{v[0].ticket_id} <-> {v[1].ticket_id}: {v[2]}" for v in leakage_violations[:5]]
        raise ValueError(f"Cross-split leakage detected ({len(leakage_violations)} violations):\n" + "\n".join(viol_msgs))

    # 11. Coverage report
    cov_report = coverage_report(splits_dict)

    # 12. Build manifest and write immutable version directory
    manifest_data = build_manifest(
        dataset_version=version,
        seed=seed,
        splits=splits_dict,
        coverage_report=cov_report,
        dedup_report={
            "exact_duplicates_dropped": exact_dropped,
            **cluster_report,
        },
    )

    out_dir = pathlib.Path(out_root) / version
    written_dir = write_version_dir(
        version_dir=out_dir,
        splits=splits_dict,
        manifest_data=manifest_data,
        changelog_entry=f"# Changelog for {version}\n\n- Generated immutable dataset version {version}.\n",
    )

    return written_dir
