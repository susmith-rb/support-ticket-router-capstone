"""Statistical methods for routing evaluation.

# REQ-REVAL-012 / REQ-REVAL-015 / REQ-REVAL-016
Pure-Python implementations of Wilson confidence intervals, cluster/group bootstrap
confidence intervals, paired bootstrap deltas, exact McNemar test, Krippendorff's
alpha (nominal), and inter-rater reliability agreement statistics.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def wilson_interval(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Calculate the Wilson score confidence interval for a binomial proportion.

    Args:
        k: Number of successes.
        n: Total number of trials.
        z: Standard normal quantile (default 1.96 for 95% confidence).

    Returns:
        (lower_bound, upper_bound) clamped to [0.0, 1.0].
    """
    if n <= 0:
        return (0.0, 0.0)
    k = max(0, min(k, n))
    p = k / n
    denominator = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    radicand = (p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)
    margin = z * math.sqrt(max(0.0, radicand))
    lower = max(0.0, (center - margin) / denominator)
    upper = min(1.0, (center + margin) / denominator)
    if k == 0:
        lower = 0.0
    if k == n:
        upper = 1.0
    return (lower, upper)


def bootstrap_ci(
    metric_fn: Callable[[List[Any]], float],
    items: Sequence[Any],
    groups: Sequence[Any],
    n_resamples: int = 1000,
    seed: int = 20261028,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Compute percentile bootstrap confidence interval resampled by group.

    Args:
        metric_fn: Callable computing a float metric over a list of items.
        items: Sequence of evaluated items.
        groups: Sequence of group identifiers matching items elementwise.
        n_resamples: Number of bootstrap iterations.
        seed: RNG seed for determinism.
        alpha: Significance level (default 0.05 for 95% CI).

    Returns:
        (lower_percentile, upper_percentile)
    """
    if len(items) != len(groups):
        raise ValueError(f"Length mismatch: {len(items)} items vs {len(groups)} groups")
    if not items:
        return (0.0, 0.0)

    group_to_indices: Dict[Any, List[int]] = {}
    for idx, grp in enumerate(groups):
        group_to_indices.setdefault(grp, []).append(idx)

    unique_groups = sorted(group_to_indices.keys(), key=lambda g: str(g))
    m = len(unique_groups)
    if m == 0:
        return (0.0, 0.0)

    rng = random.Random(seed)
    estimates: List[float] = []

    for _ in range(n_resamples):
        sampled_indices: List[int] = []
        for _ in range(m):
            grp = unique_groups[rng.randrange(m)]
            sampled_indices.extend(group_to_indices[grp])
        sampled_items = [items[i] for i in sampled_indices]
        estimates.append(float(metric_fn(sampled_items)))

    estimates.sort()
    lower_idx = int(math.floor(n_resamples * (alpha / 2.0)))
    upper_idx = int(math.floor(n_resamples * (1.0 - alpha / 2.0)))
    lower_idx = max(0, min(n_resamples - 1, lower_idx))
    upper_idx = max(0, min(n_resamples - 1, upper_idx))
    return (estimates[lower_idx], estimates[upper_idx])


def paired_bootstrap_delta(
    metric_fn: Callable[[List[Any]], float],
    items_a: Sequence[Any],
    items_b: Sequence[Any],
    groups: Sequence[Any],
    n_resamples: int = 1000,
    seed: int = 20261028,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Compute percentile bootstrap confidence interval for delta (B - A) paired by group.

    Args:
        metric_fn: Callable computing a float metric over a list of items.
        items_a: Baseline items (paired index-wise with items_b).
        items_b: Candidate items (paired index-wise with items_a).
        groups: Group identifiers matching items index-wise.
        n_resamples: Number of bootstrap iterations.
        seed: RNG seed for determinism.
        alpha: Significance level (default 0.05 for 95% CI).

    Returns:
        (delta_lower, delta_upper)
    """
    if not (len(items_a) == len(items_b) == len(groups)):
        raise ValueError(
            f"Length mismatch: len(items_a)={len(items_a)}, "
            f"len(items_b)={len(items_b)}, len(groups)={len(groups)}"
        )
    if not items_a:
        return (0.0, 0.0)

    group_to_indices: Dict[Any, List[int]] = {}
    for idx, grp in enumerate(groups):
        group_to_indices.setdefault(grp, []).append(idx)

    unique_groups = sorted(group_to_indices.keys(), key=lambda g: str(g))
    m = len(unique_groups)
    if m == 0:
        return (0.0, 0.0)

    rng = random.Random(seed)
    deltas: List[float] = []

    for _ in range(n_resamples):
        sampled_indices: List[int] = []
        for _ in range(m):
            grp = unique_groups[rng.randrange(m)]
            sampled_indices.extend(group_to_indices[grp])
        samp_a = [items_a[i] for i in sampled_indices]
        samp_b = [items_b[i] for i in sampled_indices]
        score_a = float(metric_fn(samp_a))
        score_b = float(metric_fn(samp_b))
        deltas.append(score_b - score_a)

    deltas.sort()
    lower_idx = int(math.floor(n_resamples * (alpha / 2.0)))
    upper_idx = int(math.floor(n_resamples * (1.0 - alpha / 2.0)))
    lower_idx = max(0, min(n_resamples - 1, lower_idx))
    upper_idx = max(0, min(n_resamples - 1, upper_idx))
    return (deltas[lower_idx], deltas[upper_idx])


def mcnemar_exact(b: int, c: int) -> float:
    """Compute exact two-sided McNemar test p-value from discordant pair counts.

    Under H0, discordant outcomes follow Binomial(b + c, 0.5).

    Args:
        b: Number of discordant pairs where candidate is correct and baseline is incorrect.
        c: Number of discordant pairs where baseline is correct and candidate is incorrect.

    Returns:
        Two-sided exact binomial p-value in [0.0, 1.0].
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # Exact two-sided binomial p-value under p=0.5
    cum_prob = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
    p_val = 2.0 * cum_prob
    return min(1.0, max(0.0, p_val))


def krippendorff_alpha_nominal(ratings: Sequence[Sequence[Optional[str]]]) -> float:
    """Calculate Krippendorff's alpha for nominal data with missing values.

    Args:
        ratings: Matrix of ratings where rows represent units (cases/items)
            and columns represent observers (passes/raters). Value is a string category
            or None if unrated.

    Returns:
        Alpha coefficient in [-1.0, 1.0]. Returns 1.0 for perfect agreement or degenerate inputs.
    """
    valid_units: List[List[str]] = []
    vocab: set[str] = set()

    for row in ratings:
        non_empty = [str(x) for x in row if x is not None]
        if len(non_empty) >= 2:
            valid_units.append(non_empty)
            vocab.update(non_empty)

    if not valid_units or len(vocab) <= 1:
        return 1.0

    sorted_vocab = sorted(vocab)
    o: Dict[str, Dict[str, float]] = {c: {k: 0.0 for k in sorted_vocab} for c in sorted_vocab}
    n_c: Dict[str, float] = {c: 0.0 for c in sorted_vocab}
    n_total = 0.0

    for non_empty in valid_units:
        m_u = len(non_empty)
        counts: Dict[str, int] = {}
        for val in non_empty:
            counts[val] = counts.get(val, 0) + 1

        for c, count_c in counts.items():
            n_c[c] += count_c
            n_total += count_c
            o[c][c] += (count_c * (count_c - 1)) / (m_u - 1)
            for k, count_k in counts.items():
                if c != k:
                    o[c][k] += (count_c * count_k) / (m_u - 1)

    if n_total <= 1:
        return 1.0

    sum_o_diff = sum(o[c][k] for c in sorted_vocab for k in sorted_vocab if c != k)
    d_o = sum_o_diff / n_total

    sum_nc_diff = sum(n_c[c] * n_c[k] for c in sorted_vocab for k in sorted_vocab if c != k)
    d_e = sum_nc_diff / (n_total * (n_total - 1))

    if d_e == 0.0:
        return 1.0 if d_o == 0.0 else 0.0

    return 1.0 - (d_o / d_e)


def mean_pairwise_agreement(ratings: Sequence[Sequence[Optional[str]]]) -> float:
    """Calculate mean pairwise agreement across items with >= 2 ratings.

    Args:
        ratings: Matrix of categorical ratings (rows=units, cols=raters/passes).

    Returns:
        Mean pairwise agreement in [0.0, 1.0]. Returns 1.0 if no units have >= 2 ratings.
    """
    unit_agreements: List[float] = []

    for row in ratings:
        non_empty = [str(x) for x in row if x is not None]
        m = len(non_empty)
        if m < 2:
            continue
        counts: Dict[str, int] = {}
        for val in non_empty:
            counts[val] = counts.get(val, 0) + 1
        pairs_agree = sum(c * (c - 1) // 2 for c in counts.values())
        total_pairs = m * (m - 1) // 2
        unit_agreements.append(pairs_agree / total_pairs)

    if not unit_agreements:
        return 1.0
    return sum(unit_agreements) / len(unit_agreements)


def _sample_sd(values: Sequence[float]) -> float:
    """Compute sample standard deviation with Bessel correction (0.0 for n <= 1)."""
    n = len(values)
    if n <= 1:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return math.sqrt(max(0.0, variance))


def sd_case_means_vs_pass_means(
    numeric_ratings: Sequence[Sequence[Optional[float]]],
) -> Tuple[float, float]:
    """Compute standard deviation of case means vs standard deviation of pass means.

    Args:
        numeric_ratings: Matrix of numerical ratings (rows=cases, cols=passes).

    Returns:
        (sd_case_means, sd_pass_means)
    """
    case_means: List[float] = []
    for row in numeric_ratings:
        non_empty = [float(x) for x in row if x is not None]
        if non_empty:
            case_means.append(sum(non_empty) / len(non_empty))

    sd_case = _sample_sd(case_means)

    # Transpose to get passes (columns)
    if not numeric_ratings:
        return (sd_case, 0.0)

    n_cols = max(len(row) for row in numeric_ratings)
    pass_means: List[float] = []
    for col_idx in range(n_cols):
        col_vals = [
            float(row[col_idx])
            for row in numeric_ratings
            if col_idx < len(row) and row[col_idx] is not None
        ]
        if col_vals:
            pass_means.append(sum(col_vals) / len(col_vals))

    sd_pass = _sample_sd(pass_means)
    return (sd_case, sd_pass)


def widest_case_range(numeric_ratings: Sequence[Sequence[Optional[float]]]) -> float:
    """Compute the maximum difference between ratings within a single case.

    Args:
        numeric_ratings: Matrix of numerical ratings (rows=cases, cols=passes).

    Returns:
        Max range observed across any individual case (0.0 if empty or single rating).
    """
    max_range = 0.0
    for row in numeric_ratings:
        non_empty = [float(x) for x in row if x is not None]
        if len(non_empty) >= 2:
            r = max(non_empty) - min(non_empty)
            if r > max_range:
                max_range = r
    return max_range
