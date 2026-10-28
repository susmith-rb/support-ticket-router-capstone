"""Stochastic-tier reliability analysis over k-pass evaluation traces.

# REQ-REVAL-015: Stochastic-tier reliability (k-pass).
Computes inter-pass agreement and reliability statistics across multiple
stochastic Tier-2 invocation passes:
- Nominal Krippendorff's alpha per field (intent, escalate, decision)
- Mean pairwise agreement per field
- Standard deviation of case means vs standard deviation of pass means
- Identification of unstable cases (modal share < 1.0)
- Reliability threshold flagging (default alpha >= 0.67)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from evals.routing.stats import (
    krippendorff_alpha_nominal,
    mean_pairwise_agreement,
    sd_case_means_vs_pass_means,
    widest_case_range,
)

RELIABILITY_FIELDS = ("intent", "escalate", "decision")
DEFAULT_ALPHA_THRESHOLD = 0.67


def compute(
    traces: Sequence[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute k-pass reliability metrics across stochastic traces.

    Args:
        traces: Sequence of evaluation traces (some or all may contain 'reliability').
        cfg: Optional configuration dictionary.

    Returns:
        Structured reliability summary dictionary.
    """
    cfg = cfg or {}
    rel_cfg = cfg.get("reliability", {})
    alpha_threshold = float(rel_cfg.get("alpha_min", DEFAULT_ALPHA_THRESHOLD))

    # Filter traces that have reliability passes
    rel_traces = [
        t
        for t in traces
        if isinstance(t.get("reliability"), dict)
        and isinstance(t["reliability"].get("passes"), list)
        and len(t["reliability"]["passes"]) >= 2
    ]

    n_cases = len(rel_traces)
    if n_cases == 0:
        return {
            "cases_evaluated": 0,
            "passes_per_case": 0,
            "fields": {
                f: {
                    "krippendorff_alpha": 1.0,
                    "mean_pairwise_agreement": 1.0,
                    "sd_case_means": 0.0,
                    "sd_pass_means": 0.0,
                    "widest_case_range": 0.0,
                    "is_reliable": True,
                    "unstable_cases_count": 0,
                    "unstable_cases": [],
                }
                for f in RELIABILITY_FIELDS
            },
            "all_fields_reliable": True,
            "unstable_tickets": [],
        }

    k_passes = len(rel_traces[0]["reliability"]["passes"])

    fields_summary: Dict[str, Dict[str, Any]] = {}
    all_unstable_tickets: set[str] = set()

    for field in RELIABILITY_FIELDS:
        cat_ratings: List[List[Optional[str]]] = []
        num_ratings: List[List[Optional[float]]] = []
        unstable_for_field: List[Dict[str, Any]] = []

        for t in rel_traces:
            tid = str(t.get("ticket_id", ""))
            passes = t["reliability"]["passes"]
            row_cat: List[Optional[str]] = []
            pass_vals: List[Any] = []

            for p in passes:
                val = p.get(field)
                if val is not None:
                    row_cat.append(str(val))
                    pass_vals.append(val)
                else:
                    row_cat.append(None)

            cat_ratings.append(row_cat)

            # Frequency of non-empty values
            counts: Dict[str, int] = {}
            for v_str in row_cat:
                if v_str is not None:
                    counts[v_str] = counts.get(v_str, 0) + 1

            total_rated = sum(counts.values())
            if total_rated > 0:
                modal_val, modal_count = max(counts.items(), key=lambda kv: kv[1])
                modal_share = modal_count / total_rated
            else:
                modal_val = None
                modal_share = 1.0

            if modal_share < 1.0:
                all_unstable_tickets.add(tid)
                unstable_for_field.append({
                    "ticket_id": tid,
                    "modal_label": modal_val,
                    "modal_share": round(modal_share, 4),
                    "passes": [p.get(field) for p in passes],
                })

            # Numeric encoding for SD and range:
            # - For escalate: 1.0 if True, 0.0 if False
            # - For intent/decision: 1.0 if rating == modal_val, else 0.0
            row_num: List[Optional[float]] = []
            for v in pass_vals:
                if field == "escalate":
                    row_num.append(1.0 if bool(v) else 0.0)
                else:
                    row_num.append(1.0 if str(v) == modal_val else 0.0)
            num_ratings.append(row_num)

        alpha = krippendorff_alpha_nominal(cat_ratings)
        pairwise_agree = mean_pairwise_agreement(cat_ratings)
        sd_cases, sd_passes = sd_case_means_vs_pass_means(num_ratings)
        widest_range = widest_case_range(num_ratings)

        is_reliable = alpha >= alpha_threshold

        fields_summary[field] = {
            "krippendorff_alpha": round(alpha, 4),
            "mean_pairwise_agreement": round(pairwise_agree, 4),
            "sd_case_means": round(sd_cases, 4),
            "sd_pass_means": round(sd_passes, 4),
            "widest_case_range": round(widest_range, 4),
            "is_reliable": is_reliable,
            "unstable_cases_count": len(unstable_for_field),
            "unstable_cases": unstable_for_field,
        }

    all_reliable = all(f["is_reliable"] for f in fields_summary.values())

    return {
        "cases_evaluated": n_cases,
        "passes_per_case": k_passes,
        "fields": fields_summary,
        "all_fields_reliable": all_reliable,
        "unstable_tickets": sorted(all_unstable_tickets),
    }
