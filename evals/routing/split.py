"""Deterministic group-aware dataset splitting for routing evaluations.

# REQ-REVAL-009: Deterministic group-aware splits & dataset versioning.
Provides pure function splitting logic:
- Group-aware: guarantees cases with identical group_id never cross split boundaries.
- Stratified by (source, primary scenario, intent).
- Deterministic hashing via sha256(f"{seed}:{group_id}").
- Curated regression cases preserved unconditionally.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

from evals.routing.schema import RoutingCase

DEFAULT_QUOTAS: Dict[str, int] = {
    "dev": 400,
    "heldout": 400,
    "regression": 150,
}


def assign(
    cases: Sequence[RoutingCase],
    seed: int = 20261028,
    quotas: Optional[Dict[str, int]] = None,
) -> List[RoutingCase]:
    """Assign cases to splits (dev, regression, heldout, pool) deterministically.

    Pure function:
    - Guaranteed byte-identical output across identical inputs and seed.
    - Preserves groups (near-duplicates and paraphrases travel together).
    - Preserves regression set without resampling.
    - Balances strata across dev and heldout.
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

    # 3. Stratify remaining groups by (source, primary_scenario, intent)
    strata: Dict[Tuple[str, str, str], List[Tuple[str, List[RoutingCase]]]] = {}
    for grp_id, grp_cases in non_regression_groups.items():
        # Representative member is the one with smallest ticket_id
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
            # Alternating assignment for balanced strata
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

    # 6. Construct new RoutingCase instances with updated split and group_id
    updated_cases: List[RoutingCase] = []
    for c in cases:
        grp_id = c.group_id or f"grp-{c.ticket_id}"
        assigned_split = group_assignments[grp_id]
        if c.split != assigned_split or c.group_id != grp_id:
            updated_cases.append(dataclasses.replace(c, split=assigned_split, group_id=grp_id))
        else:
            updated_cases.append(c)

    updated_cases.sort(key=lambda c: c.ticket_id)
    return updated_cases
