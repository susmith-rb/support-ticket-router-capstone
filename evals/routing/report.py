"""Immutable report artifacts and scorecard generator.

# REQ-REVAL-019: Immutable runs & report artifacts.
Generates comprehensive, byte-identical, deterministic evaluation artifacts:
- scorecard.json: Full metrics, gates, slices, robustness, reliability, clusters,
  cost/latency, and comparison
- report.md: Human-readable markdown report with headline metrics table (verified vs
  incl-provisional with bootstrap CIs), hard gates checklist, per-category F1,
  confusion matrix, robustness, reliability, failure clusters, cost/latency projections,
  per-slice performance, degraded error cases, and comparison analysis
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from evals.routing.clusters import cluster_failures, get_representative_misclassified
from evals.routing.compare import compare
from evals.routing.gates import evaluate as evaluate_gates
from evals.routing.metrics import compute as compute_metrics
from evals.routing.reliability import compute as compute_reliability
from evals.routing.robustness import compute as compute_robustness
from evals.routing.schema import RoutingCase


def _find_highest_version_dataset_dir(base_dir: Path = Path("evals/data")) -> Path:
    """Find the directory with the highest version number matching routing_v1*."""
    if not base_dir.is_dir():
        return base_dir / "routing_v1"
    matches = list(base_dir.glob("routing_v1*"))
    valid_dirs = [p for p in matches if p.is_dir()]
    if not valid_dirs:
        return base_dir / "routing_v1"

    def _sort_key(p: Path):
        nums = [int(n) for n in re.findall(r"\d+", p.name)]
        return (nums, p.name)

    valid_dirs.sort(key=_sort_key)
    return valid_dirs[-1]


def resolve_dataset_path(
    dataset_arg: str | Path | None = None,
    split: str | None = None,
    default_split: str = "dev",
) -> Path:
    """Resolve dataset path from argument, handling version directory, JSONL, or default search.

    # REQ-REVAL-019 / CLI UX:
    - If dataset_arg is a .jsonl file (or points to an existing file), return it.
    - If dataset_arg is None or empty:
      Look in evals/data for highest routing_v1.* directory.
      If not found, fallback to evals/data/routing_v1.
    - If dataset path is a directory:
      Target split is split or default_split.
      Return directory / f"{target_split}.jsonl".
    """
    target_split = split or default_split
    if not dataset_arg:
        dataset_dir = _find_highest_version_dataset_dir()
        candidate = dataset_dir / f"{target_split}.jsonl"
        return candidate if candidate.is_file() else dataset_dir

    p = Path(dataset_arg)
    if p.is_file():
        return p

    if p.is_dir():
        candidate = p / f"{target_split}.jsonl"
        if candidate.is_file():
            return candidate
        return p

    # If it points to the non-existent legacy directory name 'routing_v1'
    if p.name == "routing_v1":
        highest = _find_highest_version_dataset_dir(p.parent)
        candidate = highest / f"{target_split}.jsonl"
        if candidate.is_file():
            return candidate
        return highest

    return p


def _compute_minimal_cost_latency(
    traces: Sequence[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute fallback cost and latency statistics if evals.routing.cost is unavailable."""
    cfg = cfg or {}
    prices = cfg.get("prices", {})
    t2_prices = prices.get("router_tier2", {"input_per_million": 0.15, "output_per_million": 0.60})

    latencies_by_tier: Dict[str, List[float]] = {}
    total_latencies: List[float] = []
    total_cost = 0.0
    tier_call_counts: Dict[str, int] = {}

    for t in traces:
        t_lat = float(t.get("latency_ms_total", 0.0))
        if t_lat <= 0.0:
            t_lat = sum(float(c.get("latency_ms", 0.0)) for c in t.get("tier_calls", []))
        total_latencies.append(t_lat)

        c_cost = float(t.get("cost_usd_total", 0.0))
        if c_cost > 0.0:
            total_cost += c_cost

        for call in t.get("tier_calls", []):
            tier = str(call.get("tier", "unknown"))
            tier_call_counts[tier] = tier_call_counts.get(tier, 0) + 1
            lat = float(call.get("latency_ms", 0.0))
            latencies_by_tier.setdefault(tier, []).append(lat)

            # Estimate cost from tokens if cost_usd not present
            if c_cost == 0.0 and tier == "tier2_flash":
                usage = call.get("usage") or {}
                prompt_tok = int(usage.get("prompt_tokens", 0))
                out_tok = int(usage.get("output_tokens", 0))
                cost = (prompt_tok * float(t2_prices["input_per_million"]) / 1_000_000.0) + (
                    out_tok * float(t2_prices["output_per_million"]) / 1_000_000.0
                )
                total_cost += cost

    n = len(traces)

    def _perc(vals: List[float], p: float) -> float:
        if not vals:
            return 0.0
        sorted_vals = sorted(vals)
        k = (len(sorted_vals) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        return sorted_vals[int(f)] * (c - k) + sorted_vals[int(c)] * (k - f)

    policy_p50 = _perc(total_latencies, 0.50) / 1000.0
    policy_p95 = _perc(total_latencies, 0.95) / 1000.0
    policy_p99 = _perc(total_latencies, 0.99) / 1000.0
    policy_max = (max(total_latencies) if total_latencies else 0.0) / 1000.0

    tier_stats: Dict[str, Dict[str, Any]] = {}
    for tier, lats in sorted(latencies_by_tier.items()):
        tier_stats[tier] = {
            "calls": len(lats),
            "p50_ms": round(_perc(lats, 0.50), 2),
            "p95_ms": round(_perc(lats, 0.95), 2),
            "p99_ms": round(_perc(lats, 0.99), 2),
            "max_ms": round(max(lats) if lats else 0.0, 2),
        }

    cost_per_1k = (total_cost / n * 1000.0) if n > 0 else 0.0
    proj_1m = cost_per_1k * 1000.0
    proj_10m = cost_per_1k * 10_000.0

    return {
        "total_cost_usd": round(total_cost, 6),
        "cost_total_usd": round(total_cost, 6),
        "cost_per_1k_usd": round(cost_per_1k, 4),
        "projected_monthly_1m_usd": round(proj_1m, 2),
        "projected_monthly_10m_usd": round(proj_10m, 2),
        "policy_p50_seconds": round(policy_p50, 4),
        "policy_p95_seconds": round(policy_p95, 4),
        "policy_p99_seconds": round(policy_p99, 4),
        "policy_max_seconds": round(policy_max, 4),
        "per_tier": tier_stats,
    }


def _format_markdown_report(
    scorecard: Dict[str, Any],
    comparison: Optional[Dict[str, Any]] = None,
) -> str:
    """Format evaluation scorecard and comparison into structured deterministic Markdown."""
    lines: List[str] = []

    run_id = scorecard.get("run_id", "run_unknown")
    target = scorecard.get("target", "policy")
    created_at = scorecard.get("created_at", "2026-10-28T12:00:00Z")
    v_metrics = scorecard.get("metrics", {}).get("verified", {})
    a_metrics = scorecard.get("metrics", {}).get("all", {})
    gates_data = scorecard.get("gates", {})
    gates = gates_data.get("gates", {})
    robustness = scorecard.get("robustness", {})
    reliability = scorecard.get("reliability", {})
    cost_lat = scorecard.get("cost_latency", {})
    clusters = scorecard.get("clusters", [])
    rep_misclassified = scorecard.get("representative_misclassified", {})
    degraded_cases = scorecard.get("degraded_cases", [])
    prov = scorecard.get("provenance", {})

    overall_status = gates_data.get("overall_status")
    if overall_status is None:
        overall_status = "PASS" if gates_data.get("overall_passed") else "FAIL"

    if overall_status == "PASS":
        overall_icon = "✅ PASS"
    elif overall_status == "INCOMPLETE":
        overall_icon = "⚠️ INCOMPLETE"
    else:
        overall_icon = "❌ FAIL"

    lines.append(f"# Routing Evaluation Report — Run `{run_id}`\n")
    lines.append(f"- **Target**: `{target}`")
    lines.append(f"- **Generated At**: `{created_at}`")
    lines.append(f"- **Total Cases**: {scorecard.get('total_cases', 0)}")
    lines.append(f"- **Evaluated Cases**: {scorecard.get('evaluated_cases', 0)}")
    lines.append(f"- **Overall Hard Gates Passed**: {overall_icon}\n")

    # Data Provenance Summary
    if prov:
        lines.append("### Data Provenance Summary\n")
        l_counts = prov.get("label_status_counts", {})
        l_str = ", ".join(f"{k}: {v}" for k, v in sorted(l_counts.items()))
        gt_counts = prov.get("ground_truth_source_counts", {})
        gt_str = ", ".join(f"{k}: {v}" for k, v in sorted(gt_counts.items()))
        v_ratio = prov.get("verified_ratio", 0.0)
        v_pct = v_ratio * 100.0
        v_cases = prov.get("verified_cases", 0)
        tot_cases = prov.get("total_cases", 0)

        lines.append(f"- **Label Status**: {l_str}")
        lines.append(f"- **Ground Truth Source**: {gt_str}")
        lines.append(f"- **Verified Ratio**: {v_pct:.2f}% ({v_cases}/{tot_cases})\n")

        if prov.get("has_low_verified_warning", False):
            lines.append(
                f"> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified ({v_pct:.2f}%). "
                "Metrics incl. provisional labels pending human adjudication.\n"
            )

    # 1. Headline Metrics Table
    lines.append("## 1. Headline Performance Metrics\n")
    lines.append("| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |")
    lines.append("|---|---|---|---|---|")

    v_ci = v_metrics.get("ci_95", {})
    a_ci = a_metrics.get("ci_95", {})
    v_counts = v_metrics.get("counts", {})
    a_counts = a_metrics.get("counts", {})

    def _fmt_ci(ci_list: Optional[List[float]], val: Optional[float], n: int) -> str:
        if val is None or n == 0 or not ci_list or len(ci_list) < 2:
            return "n/a"
        return f"[{ci_list[0]:.4f}, {ci_list[1]:.4f}]"

    def _fmt_cell(val: Optional[float], n: int) -> str:
        if val is None or n == 0:
            return "n/a (n=0)"
        return f"{val:.4f} (n={n})"

    def _row(label: str, key: str) -> str:
        v_val = v_metrics.get(key)
        a_val = a_metrics.get(key)
        v_n = v_counts.get(key, 0)
        a_n = a_counts.get(key, 0)

        v_s = _fmt_cell(v_val, v_n)
        v_ci_s = _fmt_ci(v_ci.get(key), v_val, v_n)
        a_s = _fmt_cell(a_val, a_n)
        a_ci_s = _fmt_ci(a_ci.get(key), a_val, a_n)
        return f"| **{label}** | {v_s} | {v_ci_s} | {a_s} | {a_ci_s} |"

    lines.append(_row("Routing Correctness (Primary)", "routing_correctness"))
    lines.append(_row("Intent Accuracy", "intent_accuracy"))
    lines.append(_row("Lenient Intent Accuracy", "lenient_accuracy"))
    lines.append(_row("Macro-F1", "macro_f1"))
    lines.append(_row("Decision Accuracy", "decision_accuracy"))
    lines.append(_row("Escalation Recall", "escalation_recall"))

    # Strict Safety Recall
    v_ssr = v_metrics.get("strict_safety_recall")
    v_ssr_n = v_counts.get("strict_safety_recall", 0)
    v_lb = v_metrics.get("strict_safety_wilson_lb")
    if v_ssr is None or v_ssr_n == 0:
        v_ssr_str = "n/a (n=0)"
        v_ssr_ci = "n/a"
    else:
        v_lb_val = v_lb if v_lb is not None else 0.0
        v_ssr_str = f"{v_ssr:.4f} (LB: {v_lb_val:.4f}) (n={v_ssr_n})"
        v_ssr_ci = _fmt_ci(v_ci.get("strict_safety_recall"), v_ssr, v_ssr_n)

    a_ssr = a_metrics.get("strict_safety_recall")
    a_ssr_n = a_counts.get("strict_safety_recall", 0)
    a_lb = a_metrics.get("strict_safety_wilson_lb")
    if a_ssr is None or a_ssr_n == 0:
        a_ssr_str = "n/a (n=0)"
        a_ssr_ci = "n/a"
    else:
        a_lb_val = a_lb if a_lb is not None else 0.0
        a_ssr_str = f"{a_ssr:.4f} (LB: {a_lb_val:.4f}) (n={a_ssr_n})"
        a_ssr_ci = _fmt_ci(a_ci.get("strict_safety_recall"), a_ssr, a_ssr_n)

    lines.append(
        f"| **Strict Safety Recall (Wilson LB)** | {v_ssr_str} | {v_ssr_ci} | {a_ssr_str} | {a_ssr_ci} |"
    )
    lines.append(_row("Degraded Rate", "degraded_rate"))
    lines.append(_row("Error Rate", "error_rate"))
    lines.append(_row("Auto-Resolution Share", "auto_resolution_share"))
    lines.append("")

    # 2. Hard Gates Checklist
    lines.append("## 2. Hard Gates Checklist\n")
    lines.append("| Gate Name | Threshold | Actual Value | Status | Detail |")
    lines.append("|---|---|---|---|---|")
    for g_name, g_info in sorted(gates.items()):
        if not isinstance(g_info, dict) or g_name == "secondary_targets":
            continue
        g_status = g_info.get("status")
        if g_status == "NOT_EVALUATED":
            status_icon = "⚠️ NOT_EVALUATED"
            val_str = "n/a"
        elif g_info.get("passed") is True:
            status_icon = "✅ PASS"
            val_str = f"`{g_info.get('value')}`"
        else:
            status_icon = "❌ FAIL"
            val_str = f"`{g_info.get('value')}`"
        thresh_str = f"`{g_info.get('threshold')}`"
        lines.append(
            f"| `{g_name}` | {thresh_str} | {val_str} | {status_icon} | {g_info.get('detail')} |"
        )
    lines.append("")

    # Secondary Targets Table
    sec = gates.get("secondary_targets", {})
    if sec:
        lines.append("### Secondary Non-Gating Targets\n")
        lines.append("| Target Name | Threshold | Actual Value | Met | Detail |")
        lines.append("|---|---|---|---|---|")
        for s_name, s_info in sorted(sec.items()):
            icon = "✅ YES" if s_info.get("met") else "⚠️ NO"
            lines.append(
                f"| `{s_name}` | `{s_info.get('target')}` | `{s_info.get('value')}` | {icon} | {s_info.get('detail')} |"
            )
        lines.append("")

    # 3. Optimization Objective
    obj_data = scorecard.get("objective", {})
    v_obj = obj_data.get("verified")
    a_obj = obj_data.get("all")
    v_obj_str = f"`{v_obj:.4f}`" if v_obj is not None else "`n/a (n=0)`"
    a_obj_str = f"`{a_obj:.4f}`" if a_obj is not None else "`n/a (n=0)`"
    lines.append("## 3. Optimization Objective\n")
    lines.append(
        f"- **Scenario-Stratified Objective (Verified)**: {v_obj_str}\n"
        f"- **Scenario-Stratified Objective (All)**: {a_obj_str}\n"
    )

    # 4. Per-Category Intent Table
    lines.append("## 4. Per-Category Intent Breakdown\n")
    lines.append("| Intent | Support | Precision | Recall | F1 |")
    lines.append("|---|---|---|---|---|")
    per_class = a_metrics.get("per_class", {})
    for intent_name, metrics_dict in sorted(per_class.items()):
        lines.append(
            f"| `{intent_name}` | {metrics_dict.get('support', 0)} | {metrics_dict.get('precision', 0.0):.4f} | {metrics_dict.get('recall', 0.0):.4f} | {metrics_dict.get('f1', 0.0):.4f} |"
        )
    lines.append("")

    # 5. Confusion Matrix
    lines.append("## 5. Intent Confusion Matrix\n")
    cm = a_metrics.get("confusion_matrix", {})
    if cm:
        cols = sorted({c for row in cm.values() for c in row.keys()})
        lines.append("| Expected \\ Predicted | " + " | ".join(f"`{c}`" for c in cols) + " |")
        lines.append("|---|" + "|".join("---" for _ in cols) + "|")
        for exp_label in sorted(cm.keys()):
            counts = [str(cm[exp_label].get(c, 0)) for c in cols]
            lines.append(f"| `{exp_label}` | " + " | ".join(counts) + " |")
        lines.append("")

    # 6. Robustness & Security
    lines.append("## 6. Robustness & Security Integrity\n")
    lines.append(f"- **Decision Consistency across Groups**: `{robustness.get('decision_consistency', 1.0):.4f}`")
    lines.append(f"- **Intent Consistency vs Parent**: `{robustness.get('intent_consistency', 1.0):.4f}`")
    lines.append(f"- **Paraphrase Consistency**: `{robustness.get('paraphrase_consistency', 1.0):.4f}`")
    lines.append(f"- **Prompt Injection Violations**: `{robustness.get('prompt_injection_violations', 0)}`")
    lines.append(f"- **Priority Gaming Violations**: `{robustness.get('priority_gaming_violations', 0)}`")
    lines.append(f"- **Conflicting Information Accuracy**: `{robustness.get('conflicting_info_accuracy', 1.0):.4f}`\n")

    # 7. Stochastic Tier Reliability
    lines.append("## 7. Stochastic Tier-2 Reliability (k-pass)\n")
    if reliability.get("cases_evaluated", 0) > 0:
        lines.append("| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |")
        lines.append("|---|---|---|---|---|---|")
        for f_name, f_data in sorted(reliability.get("fields", {}).items()):
            r_icon = "✅ Reliable" if f_data.get("is_reliable") else "⚠️ Low Reliability"
            lines.append(
                f"| `{f_name}` | {f_data.get('krippendorff_alpha', 0.0):.4f} | {f_data.get('mean_pairwise_agreement', 0.0):.4f} | {f_data.get('sd_case_means', 0.0):.4f} | {f_data.get('sd_pass_means', 0.0):.4f} | {r_icon} |"
            )
        lines.append(f"\nUnstable cases count: `{len(reliability.get('unstable_tickets', []))}`\n")
    else:
        lines.append("No stochastic k-pass reliability data recorded in traces.\n")

    # 8. Top Failure Clusters
    lines.append("## 8. Top Failure Clusters\n")
    err_summary = scorecard.get("error_summary", [])
    if err_summary:
        lines.append("### Error Type Summary\n")
        lines.append("| Error Type | Deciding Tier | Count | Share |")
        lines.append("|---|---|---|---|")
        for item in err_summary:
            sh_pct = f"{item.get('share', 0.0) * 100.0:.2f}%"
            lines.append(f"| `{item.get('error_type')}` | `{item.get('deciding_tier')}` | {item.get('count')} | {sh_pct} |")
        lines.append("")

    if clusters:
        lines.append("### Top Failure Clusters\n")
        lines.append("| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for cl in clusters[:10]:
            sig_str = ", ".join(cl.get("signature", []))
            dec_tier = cl.get("deciding_tier", "unknown")
            lines.append(
                f"| {cl.get('cluster_id')} | `{cl.get('error_type')}` | `{cl.get('transition')}` | `{dec_tier}` | {cl.get('size')} | {cl.get('severity_weight')} | {cl.get('score')} | `{sig_str}` |"
            )
        lines.append("\n### Representative Failure Tickets\n")
        for cl in clusters[:3]:
            lines.append(f"#### Cluster {cl.get('cluster_id')} (`{cl.get('error_type')}` — `{cl.get('transition')}`)")
            for rep in cl.get("representatives", [])[:2]:
                text_clean = rep.get("description", "").replace("\n", " ").strip()[:200]
                exp = rep.get("expected", {})
                pred = rep.get("predicted", {})
                tier = rep.get("tier") or pred.get("tier", "unknown")
                lines.append(
                    f"- **{rep.get('ticket_id')}**: \"{text_clean}\" "
                    f"(Expected: `{exp.get('decision')}/{exp.get('intent')}`, "
                    f"Predicted: `{pred.get('decision')}/{pred.get('intent')}`, "
                    f"Tier: `{tier}`)"
                )
            lines.append("")

        if rep_misclassified:
            lines.append("### Representative Misclassified Tickets\n")
            for et, reps in sorted(rep_misclassified.items()):
                lines.append(f"#### `{et}`")
                for rep in reps[:3]:
                    text_clean = rep.get("description", "").replace("\n", " ").strip()[:200]
                    exp = rep.get("expected", {})
                    pred = rep.get("predicted", {})
                    tier = rep.get("tier", "unknown")
                    lines.append(
                        f"- **{rep.get('ticket_id')}**: \"{text_clean}\" "
                        f"(Expected: `{exp.get('decision')}/{exp.get('intent')}`, "
                        f"Predicted: `{pred.get('decision')}/{pred.get('intent')}`, "
                        f"Tier: `{tier}`)"
                    )
                lines.append("")
    else:
        lines.append("Zero failures observed.\n")

    # 9. Cost and Latency Table
    lines.append("## 9. Cost & Latency Performance\n")
    tot_cost = cost_lat.get("total_cost_usd", cost_lat.get("cost_total_usd", 0.0))
    dec_cost = cost_lat.get("cost_decision_usd", tot_cost)
    rel_cost = cost_lat.get("cost_reliability_usd", 0.0)
    lines.append(f"- **Total Run Cost**: `${tot_cost:.4f}`")
    lines.append(f"- **Decision Cost**: `${dec_cost:.4f}`")
    lines.append(f"- **Reliability Cost (k-pass)**: `${rel_cost:.4f}`")
    lines.append(f"- **Cost per 1,000 Tickets**: `${cost_lat.get('cost_per_1k_usd', 0.0):.4f}`")
    lines.append(f"- **Projected Monthly Cost (1M tickets)**: `${cost_lat.get('projected_monthly_1m_usd', 0.0):.2f}`")
    lines.append(f"- **Projected Monthly Cost (10M tickets)**: `${cost_lat.get('projected_monthly_10m_usd', 0.0):.2f}`")
    lines.append(
        f"- **Policy Latency p50**: `{cost_lat.get('policy_p50_seconds', 0.0):.3f}s` | "
        f"**p95**: `{cost_lat.get('policy_p95_seconds', 0.0):.3f}s` | "
        f"**p99**: `{cost_lat.get('policy_p99_seconds', 0.0):.3f}s` | "
        f"**Max**: `{cost_lat.get('policy_max_seconds', 0.0):.3f}s`\n"
    )

    per_tier = cost_lat.get("per_tier") or cost_lat.get("tiers") or {}
    if per_tier:
        lines.append("### Per-Tier Latency Breakdown\n")
        lines.append("| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |")
        lines.append("|---|---|---|---|---|---|")
        has_unmeasured_tier = False
        for tier_name, t_data in sorted(per_tier.items()):
            calls = t_data.get("calls", 0)
            lat_measured = t_data.get("latency_measured", True)
            if not lat_measured:
                has_unmeasured_tier = True
                p50 = p95 = p99 = max_lat = "n/a*"
            elif "latency_ms" in t_data:
                lat = t_data["latency_ms"]
                if lat.get("p50") is None:
                    has_unmeasured_tier = True
                    p50 = p95 = p99 = max_lat = "n/a*"
                else:
                    p50 = f"{lat.get('p50', 0.0):.2f} ms"
                    p95 = f"{lat.get('p95', 0.0):.2f} ms"
                    p99 = f"{lat.get('p99', 0.0):.2f} ms"
                    max_lat = f"{lat.get('max', 0.0):.2f} ms"
            else:
                p50 = f"{t_data.get('p50_ms', 0.0):.2f} ms"
                p95 = f"{t_data.get('p95_ms', 0.0):.2f} ms"
                p99 = f"{t_data.get('p99_ms', 0.0):.2f} ms" if "p99_ms" in t_data else "n/a"
                max_lat = f"{t_data.get('max_ms', 0.0):.2f} ms"
            lines.append(f"| `{tier_name}` | {calls} | {p50} | {p95} | {p99} | {max_lat} |")
        lines.append("")
        if has_unmeasured_tier:
            lines.append("*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*\n")

    # 10. Per-Slice Performance
    slices_data = scorecard.get("slices", {})
    if slices_data:
        lines.append("## 10. Per-Slice Performance\n")
        slice_specs = [
            ("source_dataset", "By Source Dataset"),
            ("scenario", "By Scenario"),
            ("deciding_tier", "By Deciding Tier"),
            ("label_status", "By Label Status"),
            ("laya_contaminated", "By Laya Contamination (in-distribution collision)"),
        ]
        for cat_key, cat_title in slice_specs:
            sub_dict = slices_data.get(cat_key, {})
            if not sub_dict:
                continue
            lines.append(f"### {cat_title}\n")
            lines.append("| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |")
            lines.append("|---|---|---|---|---|")
            for slice_name, slice_metrics in sorted(sub_dict.items()):
                n_cnt = slice_metrics.get("count", 0)
                rc = slice_metrics.get("routing_correctness")
                dec = slice_metrics.get("decision_accuracy")
                ia = slice_metrics.get("intent_accuracy")
                rc_s = f"{rc:.4f}" if rc is not None else "n/a (n=0)"
                dec_s = f"{dec:.4f}" if dec is not None else "n/a (n=0)"
                ia_s = f"{ia:.4f}" if ia is not None else "n/a (n=0)"
                lines.append(f"| `{slice_name}` | {n_cnt} | {rc_s} | {dec_s} | {ia_s} |")
            lines.append("")

    # 11. Degraded / Error Cases
    lines.append("## 11. Degraded / Error Cases\n")
    if degraded_cases:
        lines.append("| Ticket ID | Reason | Tier 1 Error | Tier 2 Error |")
        lines.append("|---|---|---|---|")
        for dc in degraded_cases:
            tid = dc.get("ticket_id", "unknown")
            reason = dc.get("reason", "unknown")
            s1_err = dc.get("s1_error") or "none"
            t2_err = dc.get("t2_error") or "none"
            s1_clean = str(s1_err).replace("\n", " ").strip()[:120]
            t2_clean = str(t2_err).replace("\n", " ").strip()[:120]
            lines.append(f"| `{tid}` | `{reason}` | `{s1_clean}` | `{t2_clean}` |")
        lines.append("")
    else:
        lines.append("Zero degraded or error cases encountered.\n")

    # 12. Baseline vs Candidate Comparison (if baseline given)
    if comparison:
        lines.append("## 12. Baseline vs Candidate Comparison\n")
        verdict = comparison.get("verdict", "UNKNOWN")
        v_icon = "✅" if verdict.startswith("ACCEPT") else "❌"
        lines.append(f"### Verdict: {v_icon} `{verdict}`\n")
        for reason in comparison.get("reasons", []):
            lines.append(f"- {reason}")
        lines.append("")

        lines.append("| Metric Delta | Point Estimate | 95% Paired-Bootstrap CI |")
        lines.append("|---|---|---|")
        deltas = comparison.get("deltas", {})
        for d_name, d_val in sorted(deltas.items()):
            if isinstance(d_val, dict) and "ci_95" in d_val:
                delta_est = f"{d_val.get('delta', 0.0):.4f}" if "delta" in d_val else "n/a"
                ci_str = f"[{d_val['ci_95'][0]:.4f}, {d_val['ci_95'][1]:.4f}]"
                lines.append(f"| `{d_name}` | {delta_est} | {ci_str} |")
        lines.append("")

        mcnemar = comparison.get("mcnemar", {})
        lines.append(f"- **McNemar Routing-Correctness p-value**: `{mcnemar.get('routing_correctness_p_value', 1.0):.6f}`")
        lines.append(f"- **McNemar Decision-Correctness p-value**: `{mcnemar.get('decision_correctness_p_value', 1.0):.6f}`")
        lines.append(f"- **Wins**: {comparison.get('wins_count', 0)} | **Losses**: {comparison.get('losses_count', 0)} | **Ties**: {comparison.get('ties_count', 0)}")
        lines.append(f"- **Newly Failing Regression Cases**: `{len(comparison.get('newly_failing_regression', []))}`\n")

    return "\n".join(lines)


def write_run_artifacts(
    run_dir: str | Path,
    traces: Sequence[Dict[str, Any]],
    cases: Sequence[RoutingCase],
    cfg: Optional[Dict[str, Any]] = None,
    baseline_run_dir: Optional[str | Path] = None,
    target: str = "policy",
    config_snapshot_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate and write scorecard.json and report.md deterministically to run_dir.

    Args:
        run_dir: Output run directory.
        traces: Evaluation traces.
        cases: RoutingCase dataset.
        cfg: Configuration dictionary.
        baseline_run_dir: Optional baseline directory for comparative evaluation.
        target: Execution target name (policy, replay, deployed).
        config_snapshot_extra: Optional metadata dictionary to save in config_snapshot.json.

    Returns:
        The generated scorecard dictionary.
    """
    cfg = cfg or {}
    out_dir = Path(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_id = out_dir.name

    # Determine created_at timestamp: prefer existing config_snapshot.json, then cfg, then trace, then now
    snap_path = out_dir / "config_snapshot.json"
    created_at = None
    if snap_path.is_file():
        try:
            snap_data = json.loads(snap_path.read_text(encoding="utf-8"))
            created_at = snap_data.get("created_at")
        except Exception:
            pass

    if not created_at:
        created_at = cfg.get("created_at")
    if not created_at and traces:
        created_at = traces[0].get("created_at")
    if not created_at:
        created_at = datetime.now(timezone.utc).isoformat()

    # 1. Compute core evaluation metrics
    metrics = compute_metrics(traces, cases, cfg)

    # 2. Compute robustness
    robustness = compute_robustness(traces, cases, cfg)

    # 3. Compute reliability (k-pass)
    reliability = compute_reliability(traces, cfg)

    # 4. Failure clusters & representative misclassified tickets
    clusters = cluster_failures(traces, cases, cfg)
    rep_misclassified = get_representative_misclassified(traces, cases, n_per_type=3)
    try:
        from evals.routing.clusters import get_error_type_summary

        error_summary = get_error_type_summary(traces, cases)
    except Exception:
        error_summary = []

    # 5. Cost and Latency (lazy import or fallback)
    try:
        from evals.routing.cost import breakdown as compute_cost_breakdown

        cost_latency = compute_cost_breakdown(traces, cfg)
    except (ImportError, Exception):
        cost_latency = _compute_minimal_cost_latency(traces, cfg)

    # 6. Degraded / error cases
    degraded_cases: List[Dict[str, Any]] = []
    for t in traces:
        pred = t.get("predicted", {})
        tier = pred.get("tier", "")
        is_deg = bool(pred.get("degraded", False)) or tier == "fallback_degraded"
        has_err = t.get("error") is not None or tier == "error"
        if is_deg or has_err:
            raw = t.get("decision_raw", {})
            details = raw.get("details", {})
            degraded_cases.append({
                "ticket_id": t.get("ticket_id", "unknown"),
                "reason": raw.get("reason") or details.get("reason") or ("degraded" if is_deg else "error"),
                "s1_error": details.get("s1_error") or details.get("tier1_error") or raw.get("s1_error"),
                "t2_error": details.get("t2_error") or details.get("tier2_error") or raw.get("t2_error") or t.get("error"),
            })

    # 7. Data provenance summary
    label_status_counts: Dict[str, int] = {}
    gt_source_counts: Dict[str, int] = {}
    for c in cases:
        st = str(getattr(c, "label_status", "unknown"))
        label_status_counts[st] = label_status_counts.get(st, 0) + 1
        gts = str(getattr(c, "ground_truth_source", "unknown"))
        gt_source_counts[gts] = gt_source_counts.get(gts, 0) + 1
    v_count = label_status_counts.get("verified", 0)
    tot_count = len(cases)
    v_ratio = (v_count / tot_count) if tot_count > 0 else 0.0
    provenance_summary = {
        "total_cases": tot_count,
        "verified_cases": v_count,
        "verified_ratio": round(v_ratio, 4),
        "label_status_counts": label_status_counts,
        "ground_truth_source_counts": gt_source_counts,
        "has_low_verified_warning": v_ratio < 0.50,
    }

    # Intermediate scorecard for gate evaluation
    partial_scorecard = {
        "run_id": run_id,
        "target": target,
        "created_at": created_at,
        "total_cases": metrics["total_cases"],
        "evaluated_cases": metrics["evaluated_cases"],
        "provenance": provenance_summary,
        "verified": metrics["verified"],
        "all": metrics["all"],
        "objective": metrics["objective"],
        "slices": metrics["slices"],
        "robustness": robustness,
        "reliability": reliability,
        "cost_latency": cost_latency,
        "clusters": clusters,
        "representative_misclassified": rep_misclassified,
        "error_summary": error_summary,
        "degraded_cases": degraded_cases,
    }

    # 8. Evaluate gates
    if "gate_thresholds" in cfg:
        gates_dict, overall_passed = evaluate_gates(partial_scorecard, cfg)
    else:
        gates_dict, overall_passed = {}, True
    overall_status = gates_dict.get("overall_status", "PASS" if overall_passed else "FAIL")

    # Ensure traces.jsonl is written to run_dir so runs can be compared and explained
    out_traces_path = out_dir / "traces.jsonl"
    if not out_traces_path.exists():
        with out_traces_path.open("w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # 9. Optional Baseline comparison
    comparison_data: Optional[Dict[str, Any]] = None
    if baseline_run_dir:
        b_dir = Path(baseline_run_dir)
        b_sc_path = b_dir / "scorecard.json"
        b_traces_path = b_dir / "traces.jsonl"
        if not b_traces_path.exists():
            snap_file = b_dir / "config_snapshot.json"
            if snap_file.exists():
                try:
                    snap = json.loads(snap_file.read_text(encoding="utf-8"))
                    cached_traces = Path(".cache/routing_eval/runs") / str(snap.get("run_id")) / "traces.jsonl"
                    if cached_traces.exists():
                        b_traces_path = cached_traces
                except Exception:
                    pass

        if b_sc_path.exists() and b_traces_path.exists():
            b_sc = json.loads(b_sc_path.read_text(encoding="utf-8"))
            b_traces: List[Dict[str, Any]] = []
            with b_traces_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        b_traces.append(json.loads(line))
            comparison_data = compare(b_sc, b_traces, partial_scorecard, traces, cases, cfg)

    # Complete finalized scorecard
    scorecard: Dict[str, Any] = {
        "run_id": run_id,
        "target": target,
        "created_at": created_at,
        "total_cases": metrics["total_cases"],
        "evaluated_cases": metrics["evaluated_cases"],
        "provenance": provenance_summary,
        "metrics": {
            "verified": metrics["verified"],
            "all": metrics["all"],
        },
        "gates": {
            "overall_passed": overall_passed,
            "overall_status": overall_status,
            "gates": gates_dict,
        },
        "objective": metrics["objective"],
        "slices": metrics["slices"],
        "robustness": robustness,
        "reliability": reliability,
        "cost_latency": cost_latency,
        "clusters": clusters,
        "representative_misclassified": rep_misclassified,
        "error_summary": error_summary,
        "degraded_cases": degraded_cases,
        "comparison": comparison_data,
    }

    # Save config_snapshot.json if not present
    if not snap_path.exists():
        snapshot = {
            "run_id": run_id,
            "target": target,
            "created_at": created_at,
            "cfg": cfg,
        }
        if config_snapshot_extra:
            snapshot.update(config_snapshot_extra)
        snap_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    # Write scorecard.json (deterministic sorted keys)
    scorecard_path = out_dir / "scorecard.json"
    scorecard_json_str = json.dumps(scorecard, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    scorecard_path.write_text(scorecard_json_str, encoding="utf-8")

    # Write report.md
    report_md_str = _format_markdown_report(scorecard, comparison=comparison_data) + "\n"
    report_path = out_dir / "report.md"
    report_path.write_text(report_md_str, encoding="utf-8")

    return scorecard
