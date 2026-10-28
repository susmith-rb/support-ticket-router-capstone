"""Deterministic group-aware dataset splitting for routing evaluations.

# REQ-REVAL-009: Deterministic group-aware splits & dataset versioning.
Provides pure function splitting logic:
- Group-aware: guarantees cases with identical group_id never cross split boundaries.
- Stratified by (source, primary scenario, intent).
- Deterministic hashing via sha256(f"{seed}:{group_id}").
- Curated regression cases preserved unconditionally.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
from typing import Dict, List, Optional, Sequence, Set, Tuple

from evals.routing.schema import RoutingCase, compute_content_hash

DEFAULT_QUOTAS: Dict[str, int] = {
    "dev": 400,
    "heldout": 400,
    "regression": 150,
}


def assign(
    cases: Sequence[RoutingCase],
    seed: int = 20261028,
    quotas: Optional[Dict[str, int]] = None,
    split_mix: Optional[Dict[str, Dict[str, int]]] = None,
    train_hashes: Optional[Set[str]] = None,
) -> List[RoutingCase]:
    """Assign cases to splits (dev, regression, heldout, pool) deterministically.

    Pure function:
    - Guaranteed byte-identical output across identical inputs and seed.
    - Preserves groups (near-duplicates and paraphrases travel together).
    - Preserves regression set without resampling.
    - When split_mix is provided: enforces per-source quotas, zero Laya collisions in heldout,
      and up to 120 contaminated C2 reference cases in dev.
    - Otherwise: balances strata across dev and heldout.
    - Assigns unallocated cases beyond quota to 'pool'.
    """
    effective_quotas = dict(DEFAULT_QUOTAS)
    if quotas:
        effective_quotas.update(quotas)

    target_dev = effective_quotas.get("dev", 400)
    target_heldout = effective_quotas.get("heldout", 400)

    # 1. Group cases by group_id
    groups: Dict[str, List[RoutingCase]] = {}
    for c in cases:
        grp_id = c.group_id or f"grp-{c.ticket_id}"
        groups.setdefault(grp_id, []).append(c)

    group_assignments: Dict[str, str] = {}

    # 2. Extract curated regression groups
    non_regression_groups: Dict[str, List[RoutingCase]] = {}
    for grp_id, grp_cases in groups.items():
        is_regression = any(
            c.split == "regression"
            or c.failure_ref is not None
            or "observed_failure" in (c.scenarios or ())
            for c in grp_cases
        )
        if is_regression:
            group_assignments[grp_id] = "regression"
        else:
            non_regression_groups[grp_id] = grp_cases

    if split_mix is not None:
        # Source-aware splitting per REQ-REVAL-004 / REQ-REVAL-009
        train_h = set(train_hashes or ())

        def is_group_contaminated(grp_cases: List[RoutingCase]) -> bool:
            for c in grp_cases:
                nh = compute_content_hash(c.subject, c.description)
                if nh in train_h or c.content_hash in train_h or "laya_train_collision" in (c.notes or ""):
                    return True
            return False

        def get_group_source_category(grp_cases: List[RoutingCase]) -> str:
            if len(grp_cases) > 1 or any(
                c.perturbation is not None or c.parent_ticket_id is not None or "paraphrase" in (c.scenarios or ())
                for c in grp_cases
            ):
                return "paraphrase_perturbation"
            rep = min(grp_cases, key=lambda c: c.ticket_id)
            if rep.source == "real" and (
                "fresh_unlabeled" in (rep.notes or "")
                or "fresh_llm_annotated" in (rep.notes or "")
                or (rep.source_ref and rep.source_ref.startswith("twcs_thread_"))
            ):
                return "real_fresh"
            if rep.source == "real" and (
                "retrieval_c2" in (rep.source_ref or "")
                or "c2_qid" in (rep.notes or "")
                or "golden" in (rep.source_ref or "")
            ):
                return "contaminated_c2"
            if rep.source == "public" or rep.source_dataset == "bitext_customer_support":
                return "bitext"
            if rep.source == "synthetic" and rep.source_dataset == "synthetic_template":
                return "synthetic_template"
            if rep.source == "synthetic" and rep.source_dataset == "synthetic_jetski":
                return "synthetic_jetski"
            return "other"

        by_cat: Dict[str, List[Tuple[str, List[RoutingCase], bool]]] = collections.defaultdict(list)
        for gid, grp in non_regression_groups.items():
            cat = get_group_source_category(grp)
            contam = is_group_contaminated(grp)
            by_cat[cat].append((gid, grp, contam))

        def group_hash_key(item: Tuple[str, List[RoutingCase], bool]) -> str:
            grp_id = item[0]
            return hashlib.sha256(f"{seed}:{grp_id}".encode("utf-8")).hexdigest()

        dev_counts: Dict[str, int] = collections.Counter()
        heldout_counts: Dict[str, int] = collections.Counter()

        dev_mix = split_mix.get("dev", {})
        heldout_mix = split_mix.get("heldout", {})

        for cat in ["real_fresh", "bitext", "synthetic_template", "paraphrase_perturbation", "synthetic_jetski"]:
            grp_list = by_cat.get(cat, [])
            grp_list.sort(key=group_hash_key)

            target_dev_cat = dev_mix.get(cat, 0)
            target_heldout_cat = heldout_mix.get(cat, 0)

            for gid, grp, contam in grp_list:
                gsize = len(grp)
                can_heldout = (not contam) and (heldout_counts[cat] + gsize <= target_heldout_cat)
                can_dev = (dev_counts[cat] + gsize <= target_dev_cat)

                if can_heldout and can_dev:
                    if heldout_counts[cat] <= dev_counts[cat]:
                        group_assignments[gid] = "heldout"
                        heldout_counts[cat] += gsize
                    else:
                        group_assignments[gid] = "dev"
                        dev_counts[cat] += gsize
                elif can_heldout:
                    group_assignments[gid] = "heldout"
                    heldout_counts[cat] += gsize
                elif can_dev:
                    group_assignments[gid] = "dev"
                    dev_counts[cat] += gsize
                else:
                    group_assignments[gid] = "pool"

        # If heldout total is below target_heldout (e.g. 0 uncontaminated paraphrase_perturbation),
        # fill from remaining clean pool candidates (e.g. synthetic_jetski) up to target_heldout
        total_heldout = sum(heldout_counts.values())
        if total_heldout < target_heldout:
            clean_pool_jetski = [
                item for item in by_cat.get("synthetic_jetski", [])
                if group_assignments.get(item[0]) == "pool" and not item[2]
            ]
            clean_pool_jetski.sort(key=group_hash_key)
            for gid, grp, _ in clean_pool_jetski:
                gsize = len(grp)
                if total_heldout + gsize <= target_heldout:
                    group_assignments[gid] = "heldout"
                    heldout_counts["synthetic_jetski"] += gsize
                    total_heldout += gsize

        # Allocate contaminated C2 to dev as an in-distribution reference slice (up to quota)
        target_dev_c2 = dev_mix.get("contaminated_c2", 120)
        c2_list = by_cat.get("contaminated_c2", [])
        c2_list.sort(key=group_hash_key)
        for gid, grp, _ in c2_list:
            gsize = len(grp)
            if dev_counts["contaminated_c2"] + gsize <= target_dev_c2:
                group_assignments[gid] = "dev"
                dev_counts["contaminated_c2"] += gsize
            else:
                group_assignments[gid] = "pool"

        # Any unallocated group defaults to pool
        for gid in non_regression_groups:
            if gid not in group_assignments:
                group_assignments[gid] = "pool"

    else:
        # 3. Stratify remaining groups by (source, primary_scenario, intent)
        strata: Dict[Tuple[str, str, str], List[Tuple[str, List[RoutingCase]]]] = {}
        for grp_id, grp_cases in non_regression_groups.items():
            rep = min(grp_cases, key=lambda c: c.ticket_id)
            prim_scen = rep.scenarios[0] if rep.scenarios else "normal"
            stratum_key = (rep.source, prim_scen, rep.expected.intent)
            strata.setdefault(stratum_key, []).append((grp_id, grp_cases))

        # 4. Sort strata and groups deterministically
        sorted_strata_keys = sorted(strata.keys())

        def group_hash_key(item: Tuple[str, List[RoutingCase]]) -> str:
            grp_id = item[0]
            return hashlib.sha256(f"{seed}:{grp_id}".encode("utf-8")).hexdigest()

        count_dev = 0
        count_heldout = 0

        # 5. Allocate groups to dev, heldout, or pool
        for stratum_key in sorted_strata_keys:
            grp_list = strata[stratum_key]
            grp_list.sort(key=group_hash_key)

            for idx, (grp_id, grp_cases) in enumerate(grp_list):
                grp_size = len(grp_cases)
                preferred = "dev" if (idx % 2 == 0) else "heldout"
                alternative = "heldout" if preferred == "dev" else "dev"

                assigned = "pool"
                if preferred == "dev" and count_dev + grp_size <= target_dev:
                    assigned = "dev"
                    count_dev += grp_size
                elif preferred == "heldout" and count_heldout + grp_size <= target_heldout:
                    assigned = "heldout"
                    count_heldout += grp_size
                elif alternative == "dev" and count_dev + grp_size <= target_dev:
                    assigned = "dev"
                    count_dev += grp_size
                elif alternative == "heldout" and count_heldout + grp_size <= target_heldout:
                    assigned = "heldout"
                    count_heldout += grp_size
                else:
                    assigned = "pool"

                group_assignments[grp_id] = assigned

    # Construct new RoutingCase instances with updated split and group_id
    updated_cases: List[RoutingCase] = []
    for c in cases:
        grp_id = c.group_id or f"grp-{c.ticket_id}"
        assigned_split = group_assignments.get(grp_id, "pool")
        if c.split != assigned_split or c.group_id != grp_id:
            updated_cases.append(dataclasses.replace(c, split=assigned_split, group_id=grp_id))
        else:
            updated_cases.append(c)

    if split_mix is not None:
        train_h = set(train_hashes or ())
        heldout_collisions = [
            c for c in updated_cases
            if c.split == "heldout" and (
                c.content_hash in train_h
                or compute_content_hash(c.subject, c.description) in train_h
                or "laya_train_collision" in (c.notes or "")
            )
        ]
        assert len(heldout_collisions) == 0, f"Contamination assertion failed: {len(heldout_collisions)} colliding cases in heldout"

    updated_cases.sort(key=lambda c: c.ticket_id)
    return updated_cases
