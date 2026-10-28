"""Immutable report artifacts and scorecard generator.

# REQ-REVAL-019: Immutable runs & report artifacts.
Generates comprehensive, byte-identical, deterministic evaluation artifacts:
- scorecard.json: Full metrics, gates, slices, robustness, reliability, clusters,
  cost/latency, and comparison
- report.md: Human-readable markdown report with headline metrics table (verified vs
  incl-provisional with bootstrap CIs), hard gates checklist, per-category F1,
  confusion matrix, robustness, reliability, failure clusters, cost/latency projections,
  and comparison analysis
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from evals.routing.clusters import cluster_failures
from evals.routing.compare import compare
from evals.routing.gates import evaluate as evaluate_gates
from evals.routing.metrics import compute as compute_metrics
from evals.routing.reliability import compute as compute_reliability
from evals.routing.robustness import compute as compute_robustness
from evals.routing.schema import RoutingCase


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
    policy_max = (max(total_latencies) if total_latencies else 0.0) / 1000.0

    tier_stats: Dict[str, Dict[str, float]] = {}
    for tier, lats in sorted(latencies_by_tier.items()):
        tier_stats[tier] = {
            "calls": len(lats),
            "p50_ms": round(_perc(lats, 0.50), 2),
            "p95_ms": round(_perc(lats, 0.95), 2),
            "max_ms": round(max(lats) if lats else 0.0, 2),
        }

    cost_per_1k = (total_cost / n * 1000.0) if n > 0 else 0.0
    proj_1m = cost_per_1k * 1000.0
    proj_10m = cost_per_1k * 10_000.0

    return {
        "total_cost_usd": round(total_cost, 6),
        "cost_per_1k_usd": round(cost_per_1k, 4),
        "projected_monthly_1m_usd": round(proj_1m, 2),
        "projected_monthly_10m_usd": round(proj_10m, 2),
        "policy_p50_seconds": round(policy_p50, 4),
        "policy_p95_seconds": round(policy_p95, 4),
        "policy_max_seconds": round(policy_max, 4),
        "tiers": tier_stats,
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

    lines.append(f"# Routing Evaluation Report — Run `{run_id}`\n")
    lines.append(f"- **Target**: `{target}`")
    lines.append(f"- **Generated At**: `{created_at}`")
    lines.append(f"- **Total Cases**: {scorecard.get('total_cases', 0)}")
    lines.append(f"- **Evaluated Cases**: {scorecard.get('evaluated_cases', 0)}")
    lines.append(f"- **Overall Hard Gates Passed**: {'✅ PASS' if gates_data.get('overall_passed') else '❌ FAIL'}\n")

    # 1. Headline Metrics Table
    lines.append("## 1. Headline Performance Metrics\n")
    lines.append("| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |")
    lines.append("|---|---|---|---|---|")

    v_ci = v_metrics.get("ci_95", {})
    a_ci = a_metrics.get("ci_95", {})

    def _fmt_ci(ci_list: Optional[List[float]]) -> str:
        if not ci_list or len(ci_list) < 2:
            return "n/a"
        return f"[{ci_list[0]:.4f}, {ci_list[1]:.4f}]"

    def _row(label: str, key: str, is_pct: bool = True) -> str:
        v_val = v_metrics.get(key, 0.0)
        a_val = a_metrics.get(key, 0.0)
        v_s = f"{v_val:.4f}" if is_pct else str(v_val)
        a_s = f"{a_val:.4f}" if is_pct else str(a_val)
        return f"| **{label}** | {v_s} | {_fmt_ci(v_ci.get(key))} | {a_s} | {_fmt_ci(a_ci.get(key))} |"

    lines.append(_row("Routing Correctness (Primary)", "routing_correctness"))
    lines.append(_row("Intent Accuracy", "intent_accuracy"))
    lines.append(_row("Lenient Intent Accuracy", "lenient_accuracy"))
    lines.append(_row("Macro-F1", "macro_f1"))
    lines.append(_row("Decision Accuracy", "decision_accuracy"))
    lines.append(_row("Escalation Recall", "escalation_recall"))
    lines.append(
        f"| **Strict Safety Recall (Wilson LB)** | {v_metrics.get('strict_safety_recall', 0.0):.4f} (LB: {v_metrics.get('strict_safety_wilson_lb', 0.0):.4f}) | {_fmt_ci(v_ci.get('strict_safety_recall'))} | {a_metrics.get('strict_safety_recall', 0.0):.4f} (LB: {a_metrics.get('strict_safety_wilson_lb', 0.0):.4f}) | {_fmt_ci(a_ci.get('strict_safety_recall'))} |"
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
        if g_name == "secondary_targets":
            continue
        status_icon = "✅ PASS" if g_info.get("passed") else "❌ FAIL"
        lines.append(
            f"| `{g_name}` | `{g_info.get('threshold')}` | `{g_info.get('value')}` | {status_icon} | {g_info.get('detail')} |"
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
    lines.append("## 3. Optimization Objective\n")
    lines.append(
        f"- **Scenario-Stratified Objective (Verified)**: `{obj_data.get('verified', 0.0):.4f}`\n"
        f"- **Scenario-Stratified Objective (All)**: `{obj_data.get('all', 0.0):.4f}`\n"
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
    if clusters:
        lines.append("| Rank | Error Type | Transition | Size | Severity | Score | Signature Terms |")
        lines.append("|---|---|---|---|---|---|---|")
        for cl in clusters[:10]:
            sig_str = ", ".join(cl.get("signature", []))
            lines.append(
                f"| {cl.get('cluster_id')} | `{cl.get('error_type')}` | `{cl.get('transition')}` | {cl.get('size')} | {cl.get('severity_weight')} | {cl.get('score')} | `{sig_str}` |"
            )
        lines.append("\n### Representative Failure Tickets\n")
        for cl in clusters[:3]:
            lines.append(f"#### Cluster {cl.get('cluster_id')} (`{cl.get('error_type')}` — `{cl.get('transition')}`)")
            for rep in cl.get("representatives", [])[:2]:
                text_clean = rep.get("description", "").replace("\n", " ").strip()[:200]
                lines.append(f"- **{rep.get('ticket_id')}**: \"{text_clean}\" (Expected: `{rep.get('expected', {}).get('decision')}/{rep.get('expected', {}).get('intent')}`, Predicted: `{rep.get('predicted', {}).get('decision')}/{rep.get('predicted', {}).get('intent')}`)")
            lines.append("")
    else:
        lines.append("Zero failures observed.\n")

    # 9. Cost and Latency Table
    lines.append("## 9. Cost & Latency Performance\n")
    lines.append(f"- **Total Run Cost**: `${cost_lat.get('total_cost_usd', 0.0):.4f}`")
    lines.append(f"- **Cost per 1,000 Tickets**: `${cost_lat.get('cost_per_1k_usd', 0.0):.4f}`")
    lines.append(f"- **Projected Monthly Cost (1M tickets)**: `${cost_lat.get('projected_monthly_1m_usd', 0.0):.2f}`")
    lines.append(f"- **Projected Monthly Cost (10M tickets)**: `${cost_lat.get('projected_monthly_10m_usd', 0.0):.2f}`")
    lines.append(f"- **Policy Latency p50**: `{cost_lat.get('policy_p50_seconds', 0.0):.3f}s` | **p95**: `{cost_lat.get('policy_p95_seconds', 0.0):.3f}s` | **Max**: `{cost_lat.get('policy_max_seconds', 0.0):.3f}s`\n")

    # 10. Baseline vs Candidate Comparison (if baseline given)
    if comparison:
        lines.append("## 10. Baseline vs Candidate Comparison\n")
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
    # Fixed deterministic timestamp if not provided
    created_at = cfg.get("created_at", "2026-10-28T12:00:00Z")

    # 1. Compute core evaluation metrics
    metrics = compute_metrics(traces, cases, cfg)

    # 2. Compute robustness
    robustness = compute_robustness(traces, cases, cfg)

    # 3. Compute reliability (k-pass)
    reliability = compute_reliability(traces, cfg)

    # 4. Failure clusters
    clusters = cluster_failures(traces, cases, cfg)

    # 5. Cost and Latency (lazy import or fallback)
    try:
        from evals.routing.cost import breakdown as compute_cost_breakdown

        cost_latency = compute_cost_breakdown(traces, cfg)
    except (ImportError, Exception):
        cost_latency = _compute_minimal_cost_latency(traces, cfg)

    # Intermediate scorecard for gate evaluation
    partial_scorecard = {
        "run_id": run_id,
        "target": target,
        "created_at": created_at,
        "total_cases": metrics["total_cases"],
        "evaluated_cases": metrics["evaluated_cases"],
        "verified": metrics["verified"],
        "all": metrics["all"],
        "objective": metrics["objective"],
        "slices": metrics["slices"],
        "robustness": robustness,
        "reliability": reliability,
        "cost_latency": cost_latency,
        "clusters": clusters,
    }

    # 6. Evaluate gates
    if "gate_thresholds" in cfg:
        gates_dict, overall_passed = evaluate_gates(partial_scorecard, cfg)
    else:
        gates_dict, overall_passed = {}, True

    # Ensure traces.jsonl is written to run_dir so runs can be compared and explained
    out_traces_path = out_dir / "traces.jsonl"
    if not out_traces_path.exists():
        with out_traces_path.open("w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # 7. Optional Baseline comparison
    comparison_data: Optional[Dict[str, Any]] = None
    if baseline_run_dir:
        b_dir = Path(baseline_run_dir)
        b_sc_path = b_dir / "scorecard.json"
        b_traces_path = b_dir / "traces.jsonl"
        if not b_traces_path.exists():
            snap_path = b_dir / "config_snapshot.json"
            if snap_path.exists():
                try:
                    snap = json.loads(snap_path.read_text(encoding="utf-8"))
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
        "metrics": {
            "verified": metrics["verified"],
            "all": metrics["all"],
        },
        "gates": {
            "overall_passed": overall_passed,
            "gates": gates_dict,
        },
        "objective": metrics["objective"],
        "slices": metrics["slices"],
        "robustness": robustness,
        "reliability": reliability,
        "cost_latency": cost_latency,
        "clusters": clusters,
        "comparison": comparison_data,
    }

    # Save config_snapshot.json if not present
    snap_path = out_dir / "config_snapshot.json"
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
