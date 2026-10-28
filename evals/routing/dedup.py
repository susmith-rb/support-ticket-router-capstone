"""Deduplication and leakage control for routing evaluation cases.

# REQ-REVAL-008: Deduplication & leakage control.
Provides:
1. Exact deduplication by normalized content hash with provenance-strength tie-breaking.
2. Deterministic MinHash + exact Jaccard clustering into group_id clusters.
3. Cross-split leakage detection asserting zero normalized-text and near-dup overlap.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
from typing import Any, Dict, List, Sequence, Set, Tuple

from evals.routing.provenance import PROVENANCE_STRENGTH
from evals.routing.schema import (
    RoutingCase,
    compute_content_hash,
    normalize_text,
)

_PRIME = 2147483647  # 2^31 - 1 Mersenne prime


def exact_deduplicate(cases: Sequence[RoutingCase]) -> Tuple[List[RoutingCase], int]:
    """Drop exact duplicate cases by normalized content hash.

    For duplicates, keeps the case with the strongest provenance (lowest index
    in PROVENANCE_STRENGTH) and breaks ties by lowest ticket_id lexicographically.

    Returns:
        Tuple of (retained_cases, exact_duplicates_dropped_count).
    """
    groups: Dict[str, List[RoutingCase]] = {}
    for case in cases:
        # Key by content_hash or recomputed normalized hash
        norm_hash = case.content_hash or compute_content_hash(case.subject, case.description)
        groups.setdefault(norm_hash, []).append(case)

    def provenance_key(c: RoutingCase) -> Tuple[int, str]:
        src = c.ground_truth_source
        p_rank = PROVENANCE_STRENGTH.index(src) if src in PROVENANCE_STRENGTH else len(PROVENANCE_STRENGTH)
        return (p_rank, c.ticket_id)

    retained: List[RoutingCase] = []
    dropped_count = 0

    for _norm_hash, bucket in groups.items():
        if len(bucket) > 1:
            dropped_count += len(bucket) - 1
            bucket.sort(key=provenance_key)
        retained.append(bucket[0])

    retained.sort(key=lambda c: c.ticket_id)
    return retained, dropped_count


def _get_char_5grams(text: str) -> Set[str]:
    """Extract character 5-gram shingles from normalized text."""
    norm = normalize_text(text)
    if not norm:
        return set()
    if len(norm) < 5:
        return {norm}
    return {norm[i : i + 5] for i in range(len(norm) - 4)}


def _hash_shingle(shingle: str) -> int:
    """Hash a shingle into a 31-bit positive integer."""
    digest = hashlib.md5(shingle.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def compute_minhash_signature(
    shingles: Set[str],
    permutations: Sequence[Tuple[int, int]],
) -> Tuple[int, ...]:
    """Compute deterministic MinHash signature across permutations."""
    if not shingles:
        return tuple(0 for _ in permutations)

    hashed_shingles = [_hash_shingle(s) for s in shingles]
    sig: List[int] = []
    for a, b in permutations:
        min_val = min((a * h + b) % _PRIME for h in hashed_shingles)
        sig.append(min_val)
    return tuple(sig)


def compute_jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    """Compute exact Jaccard similarity between two shingle sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return float(intersection) / float(union) if union > 0 else 0.0


class UnionFind:
    """Disjoint-set data structure for clustering connected components."""

    def __init__(self, elements: Sequence[str]):
        self.parent = {el: el for el in elements}
        self.rank = {el: 0 for el in elements}

    def find(self, x: str) -> str:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: str, y: str) -> None:
        root_x = self.find(x)
        root_y = self.find(y)
        if root_x == root_y:
            return
        if self.rank[root_x] < self.rank[root_y]:
            self.parent[root_x] = root_y
        elif self.rank[root_x] > self.rank[root_y]:
            self.parent[root_y] = root_x
        else:
            self.parent[root_y] = root_x
            self.rank[root_x] += 1


def cluster_near_duplicates(
    cases: Sequence[RoutingCase],
    seed: int = 20261028,
    num_permutations: int = 128,
    jaccard_threshold: float = 0.85,
    num_bands: int = 32,
    rows_per_band: int = 4,
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Cluster near-duplicate cases with deterministic MinHash + exact Jaccard.

    Assigns group_id to every case. If a cluster contains existing group_ids,
    the lexicographically smallest group_id is used.
    """
    assert num_bands * rows_per_band == num_permutations, "num_bands * rows_per_band must equal num_permutations"

    rng = random.Random(seed)
    permutations: List[Tuple[int, int]] = []
    for _ in range(num_permutations):
        a = rng.randint(1, _PRIME - 1)
        b = rng.randint(0, _PRIME - 1)
        permutations.append((a, b))

    case_map = {c.ticket_id: c for c in cases}
    shingles_map: Dict[str, Set[str]] = {}
    signatures: Dict[str, Tuple[int, ...]] = {}

    for c in cases:
        full_text = f"{c.subject or ''}\n{c.description}"
        shingles = _get_char_5grams(full_text)
        shingles_map[c.ticket_id] = shingles
        signatures[c.ticket_id] = compute_minhash_signature(shingles, permutations)

    # LSH banding
    buckets: Dict[Tuple[int, Tuple[int, ...]], List[str]] = {}
    for ticket_id, sig in signatures.items():
        for band_idx in range(num_bands):
            start = band_idx * rows_per_band
            band_sig = sig[start : start + rows_per_band]
            key = (band_idx, band_sig)
            buckets.setdefault(key, []).append(ticket_id)

    candidate_pairs: Set[Tuple[str, str]] = set()
    for bucket in buckets.values():
        if len(bucket) > 1:
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    id_a = bucket[i]
                    id_b = bucket[j]
                    if id_a > id_b:
                        id_a, id_b = id_b, id_a
                    candidate_pairs.add((id_a, id_b))

    uf = UnionFind([c.ticket_id for c in cases])
    confirmed_pairs: List[Tuple[str, str, float]] = []

    for id_a, id_b in sorted(candidate_pairs):
        jaccard = compute_jaccard_similarity(shingles_map[id_a], shingles_map[id_b])
        if jaccard >= jaccard_threshold:
            uf.union(id_a, id_b)
            confirmed_pairs.append((id_a, id_b, jaccard))

    # Group ticket IDs by cluster root
    clusters: Dict[str, List[str]] = {}
    for c in cases:
        root = uf.find(c.ticket_id)
        clusters.setdefault(root, []).append(c.ticket_id)

    # Assign group_id to each cluster
    new_cases: List[RoutingCase] = []
    near_dup_clusters_count = 0

    for cluster_ids in clusters.values():
        cluster_cases = [case_map[tid] for tid in cluster_ids]
        if len(cluster_cases) > 1:
            near_dup_clusters_count += 1

        existing_group_ids = [c.group_id for c in cluster_cases if c.group_id]
        if existing_group_ids:
            assigned_group_id = min(existing_group_ids)
        else:
            min_ticket_id = min(c.ticket_id for c in cluster_cases)
            assigned_group_id = f"grp-{min_ticket_id}"

        for c in cluster_cases:
            if c.group_id != assigned_group_id:
                new_cases.append(dataclasses.replace(c, group_id=assigned_group_id))
            else:
                new_cases.append(c)

    new_cases.sort(key=lambda c: c.ticket_id)
    report = {
        "near_duplicate_candidate_pairs": len(candidate_pairs),
        "near_duplicate_confirmed_pairs": len(confirmed_pairs),
        "near_duplicate_clusters": near_dup_clusters_count,
        "total_groups": len(clusters),
    }
    return new_cases, report


def deduplicate_and_cluster(
    cases: Sequence[RoutingCase],
    seed: int = 20261028,
    jaccard_threshold: float = 0.85,
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Execute end-to-end deduplication and group clustering.

    1. Exact deduplication by normalized content hash.
    2. Deterministic MinHash + exact Jaccard clustering with group_id assignment.

    Returns:
        (retained_cases, report_dict)
    """
    input_count = len(cases)
    retained_exact, exact_dropped = exact_deduplicate(cases)
    clustered_cases, cluster_report = cluster_near_duplicates(
        retained_exact,
        seed=seed,
        jaccard_threshold=jaccard_threshold,
    )

    report = {
        "input_cases": input_count,
        "exact_duplicates_dropped": exact_dropped,
        "retained_cases": len(clustered_cases),
        **cluster_report,
    }
    return clustered_cases, report


def check_leakage(
    splits: Dict[str, Sequence[RoutingCase]],
    jaccard_threshold: float = 0.85,
) -> List[Tuple[RoutingCase, RoutingCase, str]]:
    """Check for data and group leakage across splits.

    Asserts zero normalized-text overlap, zero near-dup pairs (Jaccard >= threshold),
    and zero group_id sharing across dev, regression, and heldout.
    Deliberate regression copies (failure_ref set) are allowed in regression vs dev
    only, but strictly forbidden from heldout.

    Returns:
        List of (case_a, case_b, reason) violation tuples.
    """
    violations: List[Tuple[RoutingCase, RoutingCase, str]] = []
    target_splits = [s for s in ("dev", "regression", "heldout") if s in splits]

    # Pre-extract normalized texts, shingles, and groups
    case_meta: Dict[str, Dict[str, Any]] = {}
    for split_name in target_splits:
        for c in splits[split_name]:
            norm_hash = c.content_hash or compute_content_hash(c.subject, c.description)
            full_text = f"{c.subject or ''}\n{c.description}"
            shingles = _get_char_5grams(full_text)
            case_meta[c.ticket_id] = {
                "split": split_name,
                "norm_hash": norm_hash,
                "shingles": shingles,
                "group_id": c.group_id,
                "case": c,
                "is_deliberate_regression": (split_name == "regression" and c.failure_ref is not None),
            }

    # Compare pairs of target splits
    for idx_a in range(len(target_splits)):
        for idx_b in range(idx_a + 1, len(target_splits)):
            split_a = target_splits[idx_a]
            split_b = target_splits[idx_b]

            cases_a = splits[split_a]
            cases_b = splits[split_b]

            for ca in cases_a:
                meta_a = case_meta[ca.ticket_id]
                for cb in cases_b:
                    meta_b = case_meta[cb.ticket_id]

                    # Deliberate regression copy allowance (regression vs dev only)
                    allowed_regression_copy = (
                        (meta_a["is_deliberate_regression"] and split_b == "dev")
                        or (meta_b["is_deliberate_regression"] and split_a == "dev")
                    )

                    # 1. Exact text hash match
                    if meta_a["norm_hash"] == meta_b["norm_hash"] and not allowed_regression_copy:
                        violations.append(
                            (
                                ca,
                                cb,
                                f"Exact normalized text leakage between {split_a} and {split_b}",
                            )
                        )
                        continue

                    # 2. Group ID sharing
                    if ca.group_id and ca.group_id == cb.group_id and not allowed_regression_copy:
                        violations.append(
                            (
                                ca,
                                cb,
                                f"Group ID '{ca.group_id}' shared across {split_a} and {split_b}",
                            )
                        )
                        continue

                    # 3. Near-duplicate Jaccard
                    jaccard = compute_jaccard_similarity(meta_a["shingles"], meta_b["shingles"])
                    if jaccard >= jaccard_threshold and not allowed_regression_copy:
                        violations.append(
                            (
                                ca,
                                cb,
                                f"Near-duplicate leakage (Jaccard={jaccard:.3f} >= {jaccard_threshold}) across {split_a} and {split_b}",
                            )
                        )

    return violations
