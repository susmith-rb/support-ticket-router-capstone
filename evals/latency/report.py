"""Report generation, percentile statistics, and artifact writing for latency benchmarks.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

METRIC_NAMES = [
    "client_total_ms",
    "ttfb_ms",
    "turn_ms",
    "agent_engine_overhead_ms",
    "routing_ms",
    "resolve_customer_ms",
    "upsert_ticket_ms",
    "a2a_handoff_ms",
    "faq_resolve_ms",
    "a2a_overhead_ms",
    "list_orders_ms",
    "get_order_ms",
    "search_ms",
    "rerank_ms",
    "generate_answer_ms",
    "model_armor_ms",
]


def percentile(values: Sequence[float], p: float) -> float | None:
    """Compute the p-th percentile using linear interpolation (numpy default).

    Uses linear interpolation between neighboring ranks:
        index = (p / 100) * (N - 1)
        low = floor(index), high = ceil(index), weight = index - low
        result = values[low] * (1 - weight) + values[high] * weight

    Args:
        values: Sequence of numeric values.
        p: Percentile in [0, 100].

    Returns:
        Interpolated percentile rounded to 2 decimal places, or None if empty.
    """
    cleaned = sorted(float(v) for v in values if v is not None and not math.isnan(v))
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return round(cleaned[0], 2)

    rank = (p / 100.0) * (len(cleaned) - 1)
    low_idx = int(math.floor(rank))
    high_idx = int(math.ceil(rank))
    weight = rank - low_idx

    val = cleaned[low_idx] * (1.0 - weight) + cleaned[high_idx] * weight
    return round(val, 2)


def get_next_run_dir(out_root: str | Path, label: str | None = None) -> Path:
    """Find the next monotonic run directory (run_001, run_002, ...) without overwriting."""
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)

    pattern = re.compile(r"^run_(\d{3})(?:_.*)?$")
    max_num = 0

    if root.exists():
        for entry in root.iterdir():
            if entry.is_dir():
                m = pattern.match(entry.name)
                if m:
                    max_num = max(max_num, int(m.group(1)))

    next_num = max_num + 1
    base_name = f"run_{next_num:03d}"
    if label:
        clean_label = re.sub(r"[^a-zA-Z0-9_\-]+", "_", label).strip("_")
        base_name = f"{base_name}_{clean_label}"

    run_dir = root / base_name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def compute_summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-path and overall summary statistics across benchmark records."""
    paths_seen: list[str] = []
    by_path: dict[str, list[dict[str, Any]]] = {}

    for r in records:
        p = str(r.get("path") or "unknown")
        if p not in by_path:
            paths_seen.append(p)
            by_path[p] = []
        by_path[p].append(r)

    path_summaries: dict[str, Any] = {}

    for p in paths_seen:
        recs = by_path[p]
        n = len(recs)
        errors = [r for r in recs if r.get("error_code") is not None]
        err_count = len(errors)
        err_rate = round(err_count / n, 4) if n > 0 else 0.0

        err_codes: dict[str, int] = {}
        for e in errors:
            c = str(e.get("error_code"))
            err_codes[c] = err_codes.get(c, 0) + 1

        matches = sum(
            1
            for r in recs
            if r.get("decision") and r.get("decision") == r.get("expected_decision")
        )
        dec_match_rate = round(matches / n, 4) if n > 0 else 0.0

        tier_counts: dict[str, int] = {}
        decision_counts: dict[str, int] = {}
        for r in recs:
            t = str(r.get("tier") or "unknown")
            tier_counts[t] = tier_counts.get(t, 0) + 1
            d = str(r.get("decision") or "unknown")
            decision_counts[d] = decision_counts.get(d, 0) + 1

        dominant_tier = "none"
        if tier_counts:
            # Pick highest count tier excluding "unknown" if others exist
            candidates = [k for k in tier_counts if k != "unknown"]
            if candidates:
                dominant_tier = max(candidates, key=lambda k: tier_counts[k])
            else:
                dominant_tier = max(tier_counts, key=lambda k: tier_counts[k])

        # Step and metric percentiles
        step_stats: dict[str, Any] = {}
        for m in METRIC_NAMES:
            vals = [r[m] for r in recs if r.get(m) is not None]
            step_stats[m] = {
                "count": len(vals),
                "p50": percentile(vals, 50.0),
                "p95": percentile(vals, 95.0),
                "max": round(max(vals), 2) if vals else None,
            }

        # Cold vs warm split
        cold_vals = [
            r["client_total_ms"]
            for r in recs
            if r.get("maybe_cold") and r.get("client_total_ms") is not None
        ]
        warm_vals = [
            r["client_total_ms"]
            for r in recs
            if not r.get("maybe_cold") and r.get("client_total_ms") is not None
        ]

        path_summaries[p] = {
            "n": n,
            "error_count": err_count,
            "error_rate": err_rate,
            "error_codes": err_codes,
            "decision_match_rate": dec_match_rate,
            "tier_distribution": tier_counts,
            "decision_distribution": decision_counts,
            "dominant_tier": dominant_tier,
            "metrics": step_stats,
            "cold_vs_warm": {
                "cold_count": len(cold_vals),
                "cold_p50": percentile(cold_vals, 50.0),
                "cold_p95": percentile(cold_vals, 95.0),
                "warm_count": len(warm_vals),
                "warm_p50": percentile(warm_vals, 50.0),
                "warm_p95": percentile(warm_vals, 95.0),
            },
        }

    # Overall summary
    total_n = len(records)
    total_errs = sum(1 for r in records if r.get("error_code") is not None)
    overall_err_rate = round(total_errs / total_n, 4) if total_n > 0 else 0.0
    overall_matches = sum(
        1
        for r in records
        if r.get("decision") and r.get("decision") == r.get("expected_decision")
    )
    overall_match_rate = round(overall_matches / total_n, 4) if total_n > 0 else 0.0

    overall_metrics: dict[str, Any] = {}
    for m in METRIC_NAMES:
        all_vals = [r[m] for r in records if r.get(m) is not None]
        overall_metrics[m] = {
            "count": len(all_vals),
            "p50": percentile(all_vals, 50.0),
            "p95": percentile(all_vals, 95.0),
            "max": round(max(all_vals), 2) if all_vals else None,
        }

    return {
        "total_requests": total_n,
        "total_errors": total_errs,
        "overall_error_rate": overall_err_rate,
        "overall_decision_match_rate": overall_match_rate,
        "paths": path_summaries,
        "overall_metrics": overall_metrics,
    }


def _format_metric_pair(m_dict: dict[str, Any], key: str) -> str:
    d = m_dict.get(key, {})
    p50 = d.get("p50")
    p95 = d.get("p95")
    if p50 is None:
        return "-"
    return f"{p50:.0f} / {p95:.0f} ms"


def generate_markdown_report(summary: dict[str, Any]) -> str:
    """Format benchmark summary into GitHub-style Markdown report."""
    lines: list[str] = [
        "# Live Per-Path Latency Benchmark Report",
        "",
        f"- **Total Requests**: {summary.get('total_requests', 0)}",
        f"- **Total Errors**: {summary.get('total_errors', 0)} ({summary.get('overall_error_rate', 0.0):.1%})",
        f"- **Overall Decision-Match Rate**: {summary.get('overall_decision_match_rate', 0.0):.1%}",
        "",
        "## Per-Path Performance Overview",
        "",
        "| Path | N | Errors | p50 Total | p95 Total | p50 Turn | p50 Routing | p50 GenAnswer | Dominant Tier |",
        "| :--- | -: | -: | -: | -: | -: | -: | -: | :--- |",
    ]

    paths = summary.get("paths", {})
    for path_name, p_data in paths.items():
        n = p_data.get("n", 0)
        errors = p_data.get("error_count", 0)
        metrics = p_data.get("metrics", {})

        tot_p50 = metrics.get("client_total_ms", {}).get("p50")
        tot_p95 = metrics.get("client_total_ms", {}).get("p95")
        turn_p50 = metrics.get("turn_ms", {}).get("p50")
        route_p50 = metrics.get("routing_ms", {}).get("p50")
        gen_p50 = metrics.get("generate_answer_ms", {}).get("p50")

        f_tot_p50 = f"{tot_p50:.1f} ms" if tot_p50 is not None else "-"
        f_tot_p95 = f"{tot_p95:.1f} ms" if tot_p95 is not None else "-"
        f_turn_p50 = f"{turn_p50:.1f} ms" if turn_p50 is not None else "-"
        f_route_p50 = f"{route_p50:.1f} ms" if route_p50 is not None else "-"
        f_gen_p50 = f"{gen_p50:.1f} ms" if gen_p50 is not None else "-"
        dom_tier = p_data.get("dominant_tier", "none")

        lines.append(
            f"| `{path_name}` | {n} | {errors} | {f_tot_p50} | {f_tot_p95} | {f_turn_p50} | {f_route_p50} | {f_gen_p50} | `{dom_tier}` |"
        )

    lines.extend([
        "",
        "## Cold vs. Warm Client Latency",
        "",
        "| Path | Cold Count | Cold p50 | Cold p95 | Warm Count | Warm p50 | Warm p95 |",
        "| :--- | -: | -: | -: | -: | -: | -: |",
    ])

    for path_name, p_data in paths.items():
        cw = p_data.get("cold_vs_warm", {})
        c_cnt = cw.get("cold_count", 0)
        c_p50 = f"{cw.get('cold_p50'):.1f} ms" if cw.get("cold_p50") is not None else "-"
        c_p95 = f"{cw.get('cold_p95'):.1f} ms" if cw.get("cold_p95") is not None else "-"
        w_cnt = cw.get("warm_count", 0)
        w_p50 = f"{cw.get('warm_p50'):.1f} ms" if cw.get("warm_p50") is not None else "-"
        w_p95 = f"{cw.get('warm_p95'):.1f} ms" if cw.get("warm_p95") is not None else "-"
        lines.append(f"| `{path_name}` | {c_cnt} | {c_p50} | {c_p95} | {w_cnt} | {w_p50} | {w_p95} |")

    lines.extend([
        "",
        "## Detailed Pipeline Step Breakdown (p50 / p95)",
        "",
        "| Path | TTFB | Platform Overhead | Customer Auth | Ticket Upsert | A2A Handoff | FAQ Resolve | Search | Rerank | Model Armor |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ])

    for path_name, p_data in paths.items():
        m = p_data.get("metrics", {})
        ttfb = _format_metric_pair(m, "ttfb_ms")
        ovh = _format_metric_pair(m, "agent_engine_overhead_ms")
        cust = _format_metric_pair(m, "resolve_customer_ms")
        ups = _format_metric_pair(m, "upsert_ticket_ms")
        hnd = _format_metric_pair(m, "a2a_handoff_ms")
        res = _format_metric_pair(m, "faq_resolve_ms")
        srch = _format_metric_pair(m, "search_ms")
        rrnk = _format_metric_pair(m, "rerank_ms")
        ma = _format_metric_pair(m, "model_armor_ms")
        lines.append(f"| `{path_name}` | {ttfb} | {ovh} | {cust} | {ups} | {hnd} | {res} | {srch} | {rrnk} | {ma} |")

    lines.extend([
        "",
        "## Tier & Decision Distribution",
        "",
        "| Path | Decisions | Tiers | Decision Match Rate |",
        "| :--- | :--- | :--- | -: |",
    ])

    for path_name, p_data in paths.items():
        decs = ", ".join(f"{k}: {v}" for k, v in p_data.get("decision_distribution", {}).items())
        tiers = ", ".join(f"{k}: {v}" for k, v in p_data.get("tier_distribution", {}).items())
        dm_rate = f"{p_data.get('decision_match_rate', 0.0):.1%}"
        lines.append(f"| `{path_name}` | {decs} | {tiers} | {dm_rate} |")

    lines.append("")
    return "\n".join(lines)


def write_report_artifacts(
    out_dir: str | Path,
    records: Sequence[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Path]:
    """Write requests.jsonl, summary.json, and report.md to target directory.

    Guarantees no access token or authorization header is persisted.
    """
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)

    requests_path = dest / "requests.jsonl"
    with open(requests_path, "w", encoding="utf-8") as f:
        for r in records:
            # Sanitize record defensively to ensure zero token leakage
            safe_r = {k: v for k, v in r.items() if "token" not in k.lower() and "auth" not in k.lower()}
            f.write(json.dumps(safe_r) + "\n")

    summary_path = dest / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    report_path = dest / "report.md"
    md_content = generate_markdown_report(summary)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return {
        "requests": requests_path,
        "summary": summary_path,
        "report": report_path,
    }
