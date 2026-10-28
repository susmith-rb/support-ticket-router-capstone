"""Cost and latency computation for routing evaluation harness.

# REQ-REVAL-018: Cost & latency breakdown.
Computes per-tier costs, latency distributions, deciding-tier slices,
and projected monthly operational costs at 1M and 10M tickets/month.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from evals.routing.trace import TierCallRecord, TraceRecord


def compute_percentiles(values: Sequence[float]) -> Dict[str, float]:
    """Compute standard summary percentiles and mean for latency distribution."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    vals = sorted(values)
    n = len(vals)
    mean_val = sum(vals) / n

    def pctl(p: float) -> float:
        k = (n - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, n - 1)
        return vals[f] + (vals[c] - vals[f]) * (k - f)

    return {
        "p50": round(pctl(50.0), 3),
        "p95": round(pctl(95.0), 3),
        "p99": round(pctl(99.0), 3),
        "max": round(vals[-1], 3),
        "mean": round(mean_val, 3),
    }


def call_cost(tier_call: Dict[str, Any] | TierCallRecord, cfg: Dict[str, Any]) -> float:
    """Compute dollar cost for a single tier call based on prices and usage in config.

    # REQ-REVAL-018: Tier 2 tokens x prices, Tier 1 amortized endpoint, Tier 0 $0.
    """
    call_dict = tier_call.to_dict() if hasattr(tier_call, "to_dict") else dict(tier_call)
    tier = str(call_dict.get("tier", ""))
    model_role = str(call_dict.get("model_role", ""))

    # Tier 2: Gemini Flash token pricing ($/1M tokens)
    if tier in ("tier2_flash", "router_tier2") or model_role in ("router_tier2", "tier2_annotator"):
        role_prices = cfg.get("prices", {}).get(model_role, {})
        input_pm = float(role_prices.get("input_per_million", 0.15))
        output_pm = float(role_prices.get("output_per_million", 0.60))

        usage = call_dict.get("usage") or {}
        p_tokens = int(usage.get("prompt_tokens", 0) or 0)
        o_tokens = int(usage.get("output_tokens", 0) or 0)
        return (p_tokens * input_pm + o_tokens * output_pm) / 1_000_000.0

    # Tier 1: Fine-tuned router-s1 encoder amortized endpoint cost
    if tier in ("tier1_router_s1", "router_s1") or model_role in ("router_s1", "tier1"):
        tier1_cfg = cfg.get("cost", {}).get("tier1", {}) if isinstance(cfg.get("cost"), dict) else {}
        monthly_endpoint = float(tier1_cfg.get("monthly_endpoint_usd", cfg.get("monthly_endpoint_usd", 50.0)))
        monthly_volume = float(tier1_cfg.get("monthly_volume", cfg.get("monthly_volume", 100000)))
        return (monthly_endpoint / monthly_volume) if monthly_volume > 0 else 0.0

    # Tier 0 & Model Armor: default $0
    if tier in ("tier0_rules", "t0_rules"):
        return float(cfg.get("cost", {}).get("tier0", {}).get("cost_per_call", 0.0))

    if tier == "model_armor":
        return float(cfg.get("cost", {}).get("model_armor", {}).get("cost_per_call", 0.0))

    return 0.0


def estimate_dry_run_cost(
    misses_by_tier: Dict[str, int],
    cfg: Dict[str, Any],
    default_tier2_tokens: Optional[Dict[str, int]] = None,
) -> float:
    """Pre-run cost estimation based on expected cache misses.

    # REQ-REVAL-011: Dry-run and cost-cap pre-abort check.
    """
    total = 0.0
    tier1_misses = misses_by_tier.get("tier1_router_s1", 0)
    if tier1_misses > 0:
        call_stub = {"tier": "tier1_router_s1", "model_role": "router_s1"}
        total += tier1_misses * call_cost(call_stub, cfg)

    tier2_misses = misses_by_tier.get("tier2_flash", 0)
    if tier2_misses > 0:
        tokens = default_tier2_tokens or {"prompt_tokens": 350, "output_tokens": 60}
        call_stub = {
            "tier": "tier2_flash",
            "model_role": "router_tier2",
            "usage": tokens,
        }
        total += tier2_misses * call_cost(call_stub, cfg)

    return total


def breakdown(
    traces: Sequence[TraceRecord | Dict[str, Any]],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Calculate comprehensive cost and latency breakdown.

    # REQ-REVAL-018:
    - Per-tier calls, cost total, cost per 1k tickets.
    - Latency p50/p95/p99/max per tier call and per total.
    - Sliced by deciding tier.
    - Projected monthly cost at 1M and 10M tickets/month.
    """
    n_cases = len(traces)
    trace_dicts = [t.to_dict() if hasattr(t, "to_dict") else dict(t) for t in traces]

    cost_total_usd = sum(float(t.get("cost_usd_total", 0.0)) for t in trace_dicts)
    cost_per_1k_usd = (cost_total_usd / n_cases * 1000.0) if n_cases > 0 else 0.0
    cost_per_ticket = (cost_total_usd / n_cases) if n_cases > 0 else 0.0

    total_latencies = [float(t.get("latency_ms_total", 0.0)) for t in trace_dicts]

    tier_calls_map: Dict[str, Dict[str, Any]] = {}
    by_deciding_tier_map: Dict[str, Dict[str, Any]] = {}

    for t in trace_dicts:
        # Deciding tier slice
        pred = t.get("predicted") or {}
        deciding_tier = str(pred.get("tier") or t.get("decision_raw", {}).get("tier") or "unknown")

        if deciding_tier not in by_deciding_tier_map:
            by_deciding_tier_map[deciding_tier] = {
                "count": 0,
                "cost_usd": 0.0,
                "latencies": [],
            }
        by_deciding_tier_map[deciding_tier]["count"] += 1
        by_deciding_tier_map[deciding_tier]["cost_usd"] += float(t.get("cost_usd_total", 0.0))
        by_deciding_tier_map[deciding_tier]["latencies"].append(float(t.get("latency_ms_total", 0.0)))

        # Per-tier call breakdowns
        calls = t.get("tier_calls", [])
        for c in calls:
            tier_name = str(c.get("tier", "unknown"))
            if tier_name not in tier_calls_map:
                tier_calls_map[tier_name] = {
                    "calls": 0,
                    "cost_usd": 0.0,
                    "latencies": [],
                }
            tier_calls_map[tier_name]["calls"] += 1
            tier_calls_map[tier_name]["cost_usd"] += float(c.get("cost_usd", 0.0))
            tier_calls_map[tier_name]["latencies"].append(float(c.get("latency_ms", 0.0)))

    # Assemble per_tier results
    per_tier: Dict[str, Any] = {}
    for tier_name, data in tier_calls_map.items():
        per_tier[tier_name] = {
            "calls": data["calls"],
            "cost_usd": round(data["cost_usd"], 6),
            "cost_share": round((data["cost_usd"] / cost_total_usd), 4) if cost_total_usd > 0 else 0.0,
            "latency_ms": compute_percentiles(data["latencies"]),
        }

    # Assemble deciding_tier results
    by_deciding_tier: Dict[str, Any] = {}
    for dt, data in by_deciding_tier_map.items():
        by_deciding_tier[dt] = {
            "count": data["count"],
            "share": round(data["count"] / n_cases, 4) if n_cases > 0 else 0.0,
            "cost_usd": round(data["cost_usd"], 6),
            "latency_ms_total": compute_percentiles(data["latencies"]),
        }

    return {
        "n_cases": n_cases,
        "cost_total_usd": round(cost_total_usd, 6),
        "cost_per_1k_usd": round(cost_per_1k_usd, 4),
        "cost_per_ticket_usd": round(cost_per_ticket, 8),
        "per_tier": per_tier,
        "latency_ms_total": compute_percentiles(total_latencies),
        "by_deciding_tier": by_deciding_tier,
        "projected_monthly_cost": {
            "at_1M": round(cost_per_ticket * 1_000_000.0, 2),
            "at_10M": round(cost_per_ticket * 10_000_000.0, 2),
        },
    }
