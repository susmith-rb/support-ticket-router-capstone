"""Unit tests for evaluation statistics module.

# REQ-REVAL-012: Deterministic routing metrics (Wilson, bootstrap).
# REQ-REVAL-015: Stochastic-tier reliability (k-pass Krippendorff alpha, agreement, SD).
# REQ-REVAL-016: Baseline vs candidate comparison (paired bootstrap, McNemar exact).
"""

from __future__ import annotations

import pytest

from evals.routing.stats import (
    bootstrap_ci,
    krippendorff_alpha_nominal,
    mcnemar_exact,
    mean_pairwise_agreement,
    paired_bootstrap_delta,
    sd_case_means_vs_pass_means,
    widest_case_range,
    wilson_interval,
)


def test_wilson_interval_bounds() -> None:
    # REQ-REVAL-012: Wilson score confidence interval calculation.
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low, high = wilson_interval(100, 100)
    assert high == 1.0
    assert low > 0.96

    low_zero, high_zero = wilson_interval(0, 50)
    assert low_zero == 0.0
    assert high_zero > 0.0

    low_50, high_50 = wilson_interval(50, 100)
    assert 0.40 <= low_50 <= 0.41
    assert 0.59 <= high_50 <= 0.60


def test_bootstrap_ci_deterministic_and_grouped() -> None:
    # REQ-REVAL-012: Cluster/group-level bootstrap confidence interval.
    items = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
    groups = ["g1", "g1", "g2", "g2", "g3", "g3"]

    def mean_fn(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    ci1 = bootstrap_ci(mean_fn, items, groups, n_resamples=500, seed=42)
    ci2 = bootstrap_ci(mean_fn, items, groups, n_resamples=500, seed=42)
    assert ci1 == ci2
    assert ci1[0] <= mean_fn(items) <= ci1[1]

    with pytest.raises(ValueError):
        bootstrap_ci(mean_fn, items, groups[:-1])


def test_paired_bootstrap_delta() -> None:
    # REQ-REVAL-016: Paired bootstrap confidence interval for deltas.
    items_a = [0.0, 0.0, 0.0, 0.0]
    items_b = [1.0, 1.0, 1.0, 1.0]
    groups = ["g1", "g2", "g3", "g4"]

    def mean_fn(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    low, high = paired_bootstrap_delta(mean_fn, items_a, items_b, groups, n_resamples=200, seed=20261028)
    assert low == 1.0
    assert high == 1.0


def test_mcnemar_exact() -> None:
    # REQ-REVAL-016: Exact two-sided McNemar test.
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(10, 10) == 1.0

    # b=0, c=10 -> 2 * (1/2)^10 = 2 / 1024 ~ 0.001953
    p_skew = mcnemar_exact(0, 10)
    assert abs(p_skew - (2.0 / 1024.0)) < 1e-6


def test_krippendorff_alpha_nominal_textbook() -> None:
    # REQ-REVAL-015: Textbook example from Krippendorff (2004) Content Analysis.
    ratings_classic = [
        ["1", "1", None, "1"],
        ["2", "2", "3", "2"],
        ["3", "3", "3", "3"],
        ["3", "3", "3", "3"],
        ["2", "2", "2", "2"],
        ["1", "2", "3", "4"],
        ["4", "4", "4", "4"],
        ["1", "1", "2", "1"],
        ["2", "2", "2", "2"],
        [None, "5", "5", "5"],
        [None, None, "1", "1"],
        [None, None, "2", None],
    ]
    alpha = krippendorff_alpha_nominal(ratings_classic)
    # Known textbook alpha is 0.743
    assert round(alpha, 3) == 0.743


def test_krippendorff_alpha_perfect_and_degenerate() -> None:
    # REQ-REVAL-015: Boundary conditions for nominal alpha.
    perfect = [["cat", "cat"], ["dog", "dog"], ["bird", "bird"]]
    assert krippendorff_alpha_nominal(perfect) == 1.0

    degenerate_empty = [[None, None], [None]]
    assert krippendorff_alpha_nominal(degenerate_empty) == 1.0

    single_rater = [["cat"], ["dog"]]
    assert krippendorff_alpha_nominal(single_rater) == 1.0


def test_mean_pairwise_agreement() -> None:
    # REQ-REVAL-015: Mean pairwise agreement across cases.
    perfect = [["A", "A", "A"], ["B", "B", "B"]]
    assert mean_pairwise_agreement(perfect) == 1.0

    # Row 1: A, A, B -> pairs: AA (agree), AB (disagree), AB (disagree) -> 1/3
    # Row 2: B, B, B -> 3/3 = 1.0
    mixed = [["A", "A", "B"], ["B", "B", "B"]]
    assert abs(mean_pairwise_agreement(mixed) - (1.0 / 3.0 + 1.0) / 2.0) < 1e-6


def test_sd_case_means_vs_pass_means() -> None:
    # REQ-REVAL-015: Case means SD vs Pass means SD.
    # Rows: 2 cases, 2 passes
    ratings = [
        [1.0, 1.0],  # case mean = 1.0
        [3.0, 3.0],  # case mean = 3.0
    ]
    # case means: [1.0, 3.0] -> sample sd = sqrt( ((1-2)^2 + (3-2)^2) / 1 ) = sqrt(2) ~ 1.4142
    # pass means: pass 0 = (1+3)/2=2, pass 1 = (1+3)/2=2 -> sample sd = 0.0
    sd_cases, sd_passes = sd_case_means_vs_pass_means(ratings)
    assert abs(sd_cases - 1.41421356) < 1e-4
    assert sd_passes == 0.0


def test_widest_case_range() -> None:
    # REQ-REVAL-015: Maximum range across ratings for any case.
    ratings = [
        [1.0, 1.5, 2.0],  # range = 1.0
        [0.0, 5.0, 2.0],  # range = 5.0
        [3.0, None, None],  # single rating -> range ignored
    ]
    assert widest_case_range(ratings) == 5.0
