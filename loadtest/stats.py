"""Statistical aggregation and summary calculation for load test runs.

# REQ-LOAD-001: Locust Load Testing Profiles and Cost/Quota Safeguards
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Compute the p-th percentile using standard linear interpolation.

    Args:
        values: Sequence of numeric values.
        p: Percentile between 0.0 and 100.0.

    Returns:
        Interpolated percentile rounded to 2 decimal places, or None if values is empty.
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


def compute_load_summary(
    records: Sequence[Dict[str, Any]],
    duration_s: float,
    profile_name: str = "custom",
) -> Dict[str, Any]:
    """Compute comprehensive aggregate metrics across recorded load test requests.

    Args:
        records: List of dictionaries each containing at least:
                 `latency_ms`, `success` (bool), `decision`, `tier`, `category`.
        duration_s: Total wall-clock elapsed time in seconds.
        profile_name: Identifier of the executed profile.

    Returns:
        Structured summary dictionary for JSON export.
    """
    total_requests = len(records)
    if total_requests == 0:
        return {
            "profile": profile_name,
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "error_rate_pct": 0.0,
            "duration_s": round(duration_s, 2),
            "actual_qps": 0.0,
            "latency_ms": {},
            "decisions": {},
            "tiers": {},
            "categories": {},
        }

    successful = [r for r in records if r.get("success", False)]
    failed = [r for r in records if not r.get("success", False)]

    success_count = len(successful)
    fail_count = len(failed)
    error_rate_pct = round((fail_count / total_requests) * 100.0, 2)
    actual_qps = round(total_requests / max(duration_s, 0.001), 2)

    all_latencies = [float(r["latency_ms"]) for r in records if "latency_ms" in r and r["latency_ms"] is not None]
    success_latencies = [float(r["latency_ms"]) for r in successful if "latency_ms" in r and r["latency_ms"] is not None]

    mean_lat = round(sum(all_latencies) / len(all_latencies), 2) if all_latencies else 0.0
    variance = (
        sum((x - mean_lat) ** 2 for x in all_latencies) / len(all_latencies)
        if len(all_latencies) > 1
        else 0.0
    )
    stddev_lat = round(math.sqrt(variance), 2)

    latency_stats = {
        "p50": percentile(all_latencies, 50.0),
        "p90": percentile(all_latencies, 90.0),
        "p95": percentile(all_latencies, 95.0),
        "p99": percentile(all_latencies, 99.0),
        "min": round(min(all_latencies), 2) if all_latencies else None,
        "max": round(max(all_latencies), 2) if all_latencies else None,
        "mean": mean_lat,
        "stddev": stddev_lat,
        "success_p50": percentile(success_latencies, 50.0),
        "success_p95": percentile(success_latencies, 95.0),
    }

    # Decisions breakdown
    decisions: Dict[str, Dict[str, Any]] = {}
    for r in records:
        d = str(r.get("decision") or "unknown")
        if d not in decisions:
            decisions[d] = {"count": 0, "percentage": 0.0}
        decisions[d]["count"] += 1
    for _d, info in decisions.items():
        info["percentage"] = round((info["count"] / total_requests) * 100.0, 2)

    # Tiers breakdown
    tiers: Dict[str, Dict[str, Any]] = {}
    for r in records:
        t = str(r.get("tier") or "unknown")
        if t not in tiers:
            tiers[t] = {"count": 0, "percentage": 0.0}
        tiers[t]["count"] += 1
    for _t, info in tiers.items():
        info["percentage"] = round((info["count"] / total_requests) * 100.0, 2)

    # Category breakdown
    categories: Dict[str, Dict[str, Any]] = {}
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        c = str(r.get("category") or "unknown")
        by_cat.setdefault(c, []).append(r)

    for c, c_records in by_cat.items():
        c_latencies = [float(x["latency_ms"]) for x in c_records if "latency_ms" in x and x["latency_ms"] is not None]
        c_fails = sum(1 for x in c_records if not x.get("success", False))
        categories[c] = {
            "count": len(c_records),
            "percentage": round((len(c_records) / total_requests) * 100.0, 2),
            "p50_ms": percentile(c_latencies, 50.0),
            "p95_ms": percentile(c_latencies, 95.0),
            "errors": c_fails,
        }

    return {
        "profile": profile_name,
        "total_requests": total_requests,
        "successful_requests": success_count,
        "failed_requests": fail_count,
        "error_rate_pct": error_rate_pct,
        "duration_s": round(duration_s, 2),
        "actual_qps": actual_qps,
        "latency_ms": latency_stats,
        "decisions": decisions,
        "tiers": tiers,
        "categories": categories,
    }
