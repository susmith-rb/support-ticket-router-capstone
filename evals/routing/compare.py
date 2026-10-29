"""Baseline vs Candidate comparison evaluator.

# REQ-REVAL-016: Baseline vs candidate comparison.
Pairs evaluation runs case-by-case on ticket_id, computing:
- Paired-bootstrap 95% confidence intervals on metric deltas (resampled by group_id)
- Exact McNemar tests on routing-correctness and decision-correctness discordant pairs
- Win / loss / tie case lists with ticket text and predicted choices
- Detection of newly failing regression cases
- Flagged per-slice deltas (|delta| >= 5pp and n >= 20)
- Gate status evaluation on both arms
- Cost and latency differential breakdown
- Formal verdict: ACCEPT, ACCEPT_NEUTRAL, or REJECT with actionable rationale
- Appending audit logs to evals/results/routing/hillclimb_log.jsonl
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.routing.gates import evaluate as evaluate_gates
from evals.routing.metrics import compute_objective
from evals.routing.schema import RoutingCase
from evals.routing.stats import mcnemar_exact, paired_bootstrap_delta

DEFAULT_LOG_PATH = Path("evals/results/routing/hillclimb_log.jsonl")


def _is_routing_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
    pred = t.get("predicted", {})
    if pred.get("decision") != c.expected.decision:
        return False
    if c.expected.decision == "auto_rag":
        return pred.get("intent") in c.expected.acceptable_intents
    if c.expected.decision == "escalate":
        return pred.get("queue") == c.expected.queue
    if c.expected.decision == "reject_oos":
        return pred.get("queue") == "none"
    return False


def _is_decision_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
    return t.get("predicted", {}).get("decision") == c.expected.decision


def _is_intent_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
    return t.get("predicted", {}).get("intent") == c.expected.intent


def compare(
    baseline_scorecard: Dict[str, Any],
    baseline_traces: Sequence[Dict[str, Any]],
    candidate_scorecard: Dict[str, Any],
    candidate_traces: Sequence[Dict[str, Any]],
    cases: Sequence[RoutingCase],
    cfg: Optional[Dict[str, Any]] = None,
    log_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Perform comprehensive paired baseline vs candidate evaluation.

    Args:
        baseline_scorecard: Baseline scorecard dictionary.
        baseline_traces: Traces produced by baseline run.
        candidate_scorecard: Candidate scorecard dictionary.
        candidate_traces: Traces produced by candidate run.
        cases: RoutingCase dataset.
        cfg: Configuration dictionary containing gate thresholds and secondary targets.
        log_path: Path to hillclimb log JSONL (optional).

    Returns:
        Structured comparison result dictionary.
    """
    cfg = cfg or {}
    seed = int(cfg.get("seed", 20261028))
    n_bootstrap = int(cfg.get("bootstrap_resamples", 1000))

    case_map = {c.ticket_id: c for c in cases if c.label_status != "rejected"}
    b_trace_map = {t["ticket_id"]: t for t in baseline_traces if "ticket_id" in t}
    c_trace_map = {t["ticket_id"]: t for t in candidate_traces if "ticket_id" in t}

    # Common tickets evaluated in both arms
    common_tids = sorted([
        tid for tid in b_trace_map if tid in c_trace_map and tid in case_map
    ])

    paired_cases = [case_map[tid] for tid in common_tids]
    paired_b_traces = [b_trace_map[tid] for tid in common_tids]
    paired_c_traces = [c_trace_map[tid] for tid in common_tids]
    paired_groups = [c.group_id for c in paired_cases]

    # 1. Win / Loss / Tie analysis on routing correctness
    wins: List[Dict[str, Any]] = []
    losses: List[Dict[str, Any]] = []
    ties: List[Dict[str, Any]] = []

    b_rc_correct = 0
    c_rc_correct = 0

    newly_failing_regression: List[Dict[str, Any]] = []

    for c, b_t, c_t in zip(paired_cases, paired_b_traces, paired_c_traces):
        b_corr = _is_routing_correct(c, b_t)
        c_corr = _is_routing_correct(c, c_t)

        if b_corr:
            b_rc_correct += 1
        if c_corr:
            c_rc_correct += 1

        rec = {
            "ticket_id": c.ticket_id,
            "split": c.split,
            "text": c.description[:200],
            "expected": c.expected.to_dict(),
            "baseline_predicted": b_t.get("predicted"),
            "candidate_predicted": c_t.get("predicted"),
        }

        if c_corr and not b_corr:
            wins.append(rec)
        elif b_corr and not c_corr:
            losses.append(rec)
            if c.split == "regression":
                newly_failing_regression.append(rec)
        else:
            ties.append(rec)

    n_paired = len(common_tids)

    # 2. McNemar test on discordant pairs
    b_discordant = len(wins)
    c_discordant = len(losses)
    p_mcnemar_rc = mcnemar_exact(b_discordant, c_discordant)

    # Decision correctness McNemar
    b_dec_wins = 0
    c_dec_wins = 0
    for c, b_t, c_t in zip(paired_cases, paired_b_traces, paired_c_traces):
        b_d = _is_decision_correct(c, b_t)
        c_d = _is_decision_correct(c, c_t)
        if c_d and not b_d:
            b_dec_wins += 1
        elif b_d and not c_d:
            c_dec_wins += 1
    p_mcnemar_dec = mcnemar_exact(b_dec_wins, c_dec_wins)

    # 3. Paired-bootstrap 95% CIs on metric deltas
    def _metric_fn_rc(trace_sample: Sequence[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
        if not trace_sample:
            return 0.0
        return sum(1 for c, t in trace_sample if _is_routing_correct(c, t)) / len(trace_sample)

    def _metric_fn_dec(trace_sample: Sequence[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
        if not trace_sample:
            return 0.0
        return sum(1 for c, t in trace_sample if _is_decision_correct(c, t)) / len(trace_sample)

    def _metric_fn_intent(trace_sample: Sequence[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
        if not trace_sample:
            return 0.0
        return sum(1 for c, t in trace_sample if _is_intent_correct(c, t)) / len(trace_sample)

    paired_b_items = list(zip(paired_cases, paired_b_traces))
    paired_c_items = list(zip(paired_cases, paired_c_traces))

    rc_ci = paired_bootstrap_delta(
        _metric_fn_rc, paired_b_items, paired_c_items, paired_groups, n_resamples=n_bootstrap, seed=seed
    )
    dec_ci = paired_bootstrap_delta(
        _metric_fn_dec, paired_b_items, paired_c_items, paired_groups, n_resamples=n_bootstrap, seed=seed
    )
    intent_ci = paired_bootstrap_delta(
        _metric_fn_intent, paired_b_items, paired_c_items, paired_groups, n_resamples=n_bootstrap, seed=seed
    )

    rc_delta = (c_rc_correct - b_rc_correct) / n_paired if n_paired > 0 else 0.0

    # Objective Delta
    b_obj = float(
        baseline_scorecard.get("objective", {}).get("all")
        or baseline_scorecard.get("objective", {}).get("verified", 0.0)
    )
    c_obj = float(
        candidate_scorecard.get("objective", {}).get("all")
        or candidate_scorecard.get("objective", {}).get("verified", 0.0)
    )
    obj_delta = c_obj - b_obj

    def _metric_fn_obj(trace_sample: Sequence[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
        obj_val, _ = compute_objective(trace_sample, is_verified_view=False)
        return obj_val

    obj_ci = paired_bootstrap_delta(
        _metric_fn_obj, paired_b_items, paired_c_items, paired_groups, n_resamples=n_bootstrap, seed=seed
    )

    # 4. Gate status for both arms. Prefer the gates already computed and stored in the baseline
    # scorecard (re-evaluating a loaded scorecard can miss in-memory-only fields and yield n=0).
    stored_b = baseline_scorecard.get("gates") if isinstance(baseline_scorecard, dict) else None
    if isinstance(stored_b, dict) and isinstance(stored_b.get("gates"), dict):
        b_gates, b_passed = stored_b["gates"], bool(stored_b.get("overall_passed"))
    else:
        b_gates, b_passed = evaluate_gates(baseline_scorecard, cfg) if "gate_thresholds" in cfg else ({}, True)

    stored_c = candidate_scorecard.get("gates") if isinstance(candidate_scorecard, dict) else None
    if isinstance(stored_c, dict) and isinstance(stored_c.get("gates"), dict):
        c_gates, c_passed = stored_c["gates"], bool(stored_c.get("overall_passed"))
    else:
        c_gates, c_passed = evaluate_gates(candidate_scorecard, cfg) if "gate_thresholds" in cfg else ({}, True)

    # 5. Cost and Latency deltas
    b_cost = float(baseline_scorecard.get("cost_latency", {}).get("total_cost_usd", 0.0))
    c_cost = float(candidate_scorecard.get("cost_latency", {}).get("total_cost_usd", 0.0))
    cost_delta = c_cost - b_cost

    b_lat = float(baseline_scorecard.get("cost_latency", {}).get("policy_p95_seconds", 0.0))
    c_lat = float(candidate_scorecard.get("cost_latency", {}).get("policy_p95_seconds", 0.0))
    lat_delta = c_lat - b_lat

    cost_better = c_cost < b_cost
    lat_better = c_lat < b_lat

    # 6. Per-slice deltas flagged (|delta| >= 5pp and n >= 20)
    flagged_slices: List[Dict[str, Any]] = []
    b_slices = baseline_scorecard.get("slices", {})
    c_slices = candidate_scorecard.get("slices", {})

    for dim, b_dim_dict in b_slices.items():
        c_dim_dict = c_slices.get(dim, {})
        for s_key, b_s_data in b_dim_dict.items():
            if s_key in c_dim_dict:
                c_s_data = c_dim_dict[s_key]
                s_count = c_s_data.get("count", 0)
                b_rc = b_s_data.get("routing_correctness", 0.0)
                c_rc = c_s_data.get("routing_correctness", 0.0)
                d_rc = c_rc - b_rc
                if abs(d_rc) >= 0.05 and s_count >= 20:
                    flagged_slices.append({
                        "dimension": dim,
                        "slice": s_key,
                        "count": s_count,
                        "baseline_routing_correctness": round(b_rc, 4),
                        "candidate_routing_correctness": round(c_rc, 4),
                        "delta": round(d_rc, 4),
                    })

    # 7. Verdict logic per REQ-REVAL-016
    reasons: List[str] = []
    verdict = "REJECT"

    gate_classes: Dict[str, str] = {}
    if not c_passed:
        reasons.append("Candidate failed hard gates.")
        b_gates_map = b_gates if isinstance(b_gates, dict) else {}
        for g_name, g_info in c_gates.items():
            # Gate entries are dicts; skip summary/status scalars (e.g. overall status strings).
            if g_name == "secondary_targets" or not isinstance(g_info, dict):
                continue
            status = g_info.get("status")
            if status == "NOT_EVALUATED":
                gate_classes[g_name] = "NOT_EVALUATED"
            elif status == "FAIL" or not g_info.get("passed", True):
                b_info = b_gates_map.get(g_name)
                b_failed = isinstance(b_info, dict) and (
                    b_info.get("status") == "FAIL"
                    or (b_info.get("status") != "NOT_EVALUATED" and not b_info.get("passed", True))
                )
                gate_classes[g_name] = "PRE_EXISTING" if b_failed else "NEW"
            else:
                continue
            reasons.append(f"Gate {gate_classes[g_name]}: {g_name} - {g_info.get('detail')}")

    if newly_failing_regression:
        reasons.append(
            f"Candidate introduced {len(newly_failing_regression)} newly failing regression cases."
        )

    # REQ-REVAL-016: improvements must not be hidden by gate failures inherited from the baseline.
    if (
        not c_passed
        and gate_classes
        and all(v in ("PRE_EXISTING", "NOT_EVALUATED") for v in gate_classes.values())
        and not newly_failing_regression
        and obj_ci[0] > 0.0
    ):
        verdict = "REJECT_BLOCKED_PREEXISTING"
        reasons.append(
            f"Improves the objective (delta={obj_delta:.4f}, 95% CI=[{obj_ci[0]:.4f}, {obj_ci[1]:.4f}]) "
            "and introduces no new gate failures; blocked only by gates that also fail or are not "
            "evaluated in the baseline."
        )

    if c_passed and not newly_failing_regression:
        # ACCEPT: hard gates pass AND objective delta CI lower bound > 0
        if obj_ci[0] > 0.0:
            verdict = "ACCEPT"
            reasons.append(
                f"Candidate passed all gates with statistically significant objective improvement: "
                f"delta={obj_delta:.4f}, 95% CI=[{obj_ci[0]:.4f}, {obj_ci[1]:.4f}]."
            )
        # ACCEPT_NEUTRAL: hard gates pass, objective delta CI spans 0, cost or latency strictly better
        elif obj_ci[0] <= 0.0 <= obj_ci[1] and (cost_better or lat_better):
            verdict = "ACCEPT_NEUTRAL"
            reasons.append(
                f"Candidate passed all gates with neutral objective delta (CI spans 0) "
                f"and strictly improved efficiency: cost_better={cost_better}, latency_better={lat_better}."
            )
        else:
            verdict = "REJECT"
            reasons.append(
                f"Objective delta not statistically positive (CI=[{obj_ci[0]:.4f}, {obj_ci[1]:.4f}]) "
                "and no efficiency improvement observed."
            )

    result = {
        "paired_cases_count": n_paired,
        "wins_count": len(wins),
        "losses_count": len(losses),
        "ties_count": len(ties),
        "wins": wins[:15],
        "losses": losses[:15],
        "newly_failing_regression": newly_failing_regression,
        "mcnemar": {
            "routing_correctness_p_value": round(p_mcnemar_rc, 6),
            "routing_correctness_discordant": {"candidate_wins": b_discordant, "baseline_wins": c_discordant},
            "decision_correctness_p_value": round(p_mcnemar_dec, 6),
            "decision_correctness_discordant": {"candidate_wins": b_dec_wins, "baseline_wins": c_dec_wins},
        },
        "deltas": {
            "routing_correctness": {
                "delta": round(rc_delta, 4),
                "ci_95": [round(rc_ci[0], 4), round(rc_ci[1], 4)],
            },
            "decision_accuracy": {
                "ci_95": [round(dec_ci[0], 4), round(dec_ci[1], 4)],
            },
            "intent_accuracy": {
                "ci_95": [round(intent_ci[0], 4), round(intent_ci[1], 4)],
            },
            "objective": {
                "delta": round(obj_delta, 4),
                "ci_95": [round(obj_ci[0], 4), round(obj_ci[1], 4)],
            },
            "total_cost_usd": round(cost_delta, 6),
            "policy_p95_seconds": round(lat_delta, 4),
        },
        "gates": {
            "baseline": {"passed": b_passed, "details": b_gates},
            "candidate": {"passed": c_passed, "details": c_gates},
        },
        "flagged_slices": flagged_slices,
        "verdict": verdict,
        "gate_classes": gate_classes,
        "reasons": reasons,
    }

    # 8. Append to hillclimb log if explicitly requested
    if log_path is not None:
        target_log = Path(log_path)
        try:
            target_log.parent.mkdir(parents=True, exist_ok=True)
            attempt_id = 1
            if target_log.exists():
                with target_log.open("r", encoding="utf-8") as f:
                    attempt_id = sum(1 for line in f if line.strip()) + 1

            cand_name = str(candidate_scorecard.get("candidate", "candidate"))
            b_run = str(baseline_scorecard.get("run_id", "baseline"))
            c_run = str(candidate_scorecard.get("run_id", "candidate"))
            git_sha = str(candidate_scorecard.get("git_sha", "unknown"))

            entry = {
                "attempt_id": attempt_id,
                "candidate": cand_name,
                "knob": str(candidate_scorecard.get("knob", "threshold_overlay")),
                "baseline_run": b_run,
                "candidate_run": c_run,
                "objective_delta": round(obj_delta, 4),
                "ci": [round(obj_ci[0], 4), round(obj_ci[1], 4)],
                "verdict": verdict,
                "git_sha": git_sha,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            with target_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Graceful fallback if logging path is unwriteable in tests
            pass

    return result
