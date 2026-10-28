"""Deterministic evaluation metrics calculator for routing evaluation harness.

# REQ-REVAL-012: Deterministic routing metrics.
Computes intent accuracy, lenient accuracy, macro-F1, confusion matrices,
decision accuracy, escalation P/R/F1, missed/false escalation rates, queue accuracy,
urgent queue recall, strict safety recall with Wilson lower bounds, routing correctness,
system tier shares, degraded/error rates, Expected Calibration Error (ECE),
fine-grained slices, and group-aware bootstrap confidence intervals.
Scored across both verified ground-truth and all non-rejected cases.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.routing.schema import RoutingCase
from evals.routing.stats import bootstrap_ci, wilson_interval
from ticket_router.config.taxonomy import load_taxonomy


def _parse_urgency_to_int(urgency: Any) -> int:
    """Map router urgency string or integer to integer level 0..3."""
    if isinstance(urgency, int):
        return max(0, min(3, urgency))
    if not urgency or not isinstance(urgency, str):
        return 0
    u_lower = urgency.strip().lower()
    if u_lower.isdigit():
        return max(0, min(3, int(u_lower)))
    if u_lower == "immediate":
        return 3
    if u_lower in ("within hours", "high", "p1", "urgent"):
        return 2
    if u_lower in ("within a day", "medium", "p2"):
        return 1
    if u_lower in ("can wait days", "normal", "low", "p3"):
        return 0
    return 0


def _compute_ece(confidences: Sequence[float], correctnesses: Sequence[bool], n_bins: int = 10) -> float:
    """Compute Expected Calibration Error (ECE) over equal-width bins."""
    if not confidences:
        return 0.0

    total_samples = len(confidences)
    bin_size = 1.0 / n_bins
    ece = 0.0

    for b in range(n_bins):
        low = b * bin_size
        high = (b + 1) * bin_size
        # Last bin includes upper boundary 1.0
        bin_indices = [
            i
            for i, c in enumerate(confidences)
            if (low <= c < high) or (b == n_bins - 1 and low <= c <= high)
        ]
        count_b = len(bin_indices)
        if count_b == 0:
            continue

        acc_b = sum(1.0 for i in bin_indices if correctnesses[i]) / count_b
        conf_b = sum(confidences[i] for i in bin_indices) / count_b
        ece += (count_b / total_samples) * abs(acc_b - conf_b)

    return ece


def _compute_view_metrics(
    matched_pairs: Sequence[Tuple[RoutingCase, Dict[str, Any]]],
    taxonomy_intents: Sequence[str],
    is_verified_view: bool,
    seed: int = 20261028,
    n_bootstrap: int = 1000,
) -> Dict[str, Any]:
    """Compute metrics for either verified-only or all non-rejected view."""
    # Filter cases eligible for scoring in this view
    eligible_pairs: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for case, trace in matched_pairs:
        if is_verified_view:
            if case.label_status == "verified":
                eligible_pairs.append((case, trace))
        else:
            eligible_pairs.append((case, trace))

    n_cases = len(eligible_pairs)
    if n_cases == 0:
        return {
            "count": 0,
            "routing_correctness": 0.0,
            "ci_95": {
                "routing_correctness": [0.0, 0.0],
                "intent_accuracy": [0.0, 0.0],
                "decision_accuracy": [0.0, 0.0],
                "strict_safety_recall": [0.0, 0.0],
                "macro_f1": [0.0, 0.0],
            },
            "intent_accuracy": 0.0,
            "lenient_accuracy": 0.0,
            "macro_f1": 0.0,
            "per_class": {},
            "confusion_matrix": {},
            "taxonomy_validity_rate": 0.0,
            "decision_accuracy": 0.0,
            "escalation_precision": 0.0,
            "escalation_recall": 0.0,
            "escalation_f1": 0.0,
            "missed_escalation_rate": 0.0,
            "false_escalation_rate": 0.0,
            "queue_accuracy": 0.0,
            "urgent_queue_recall": 0.0,
            "strict_safety_recall": 0.0,
            "strict_safety_wilson_lb": 0.0,
            "strict_safety_total": 0,
            "strict_safety_passed": 0,
            "tier_share": {},
            "tier2_invocation_rate": 0.0,
            "degraded_rate": 0.0,
            "error_rate": 0.0,
            "auto_resolution_share": 0.0,
            "ece": 0.0,
        }

    # 1. Intent metrics
    intent_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if not is_verified_view or "intent" in c.verified_fields:
            intent_eligible.append((c, t))

    exact_intent_correct = 0
    lenient_intent_correct = 0
    confusion: Dict[str, Dict[str, int]] = {
        exp: {pred: 0 for pred in list(taxonomy_intents) + ["other_invalid"]}
        for exp in taxonomy_intents
    }

    t1_confs: List[float] = []
    t1_correct: List[bool] = []

    for c, t in intent_eligible:
        p_intent = str(t.get("predicted", {}).get("intent", ""))
        exp_intent = c.expected.intent
        is_exact = p_intent == exp_intent
        is_lenient = p_intent in c.expected.acceptable_intents

        if is_exact:
            exact_intent_correct += 1
        if is_lenient:
            lenient_intent_correct += 1

        pred_col = p_intent if p_intent in taxonomy_intents else "other_invalid"
        if exp_intent in confusion:
            confusion[exp_intent][pred_col] = confusion[exp_intent].get(pred_col, 0) + 1

        # Check for Tier-1 calibration (ECE)
        p_tier = t.get("predicted", {}).get("tier", "")
        if p_tier == "tier1_router_s1":
            prob = float(t.get("decision_raw", {}).get("details", {}).get("intent_prob", 0.0))
            t1_confs.append(prob)
            t1_correct.append(is_exact)

    n_intent = len(intent_eligible)
    intent_acc = (exact_intent_correct / n_intent) if n_intent > 0 else 0.0
    lenient_acc = (lenient_intent_correct / n_intent) if n_intent > 0 else 0.0
    ece = _compute_ece(t1_confs, t1_correct, n_bins=10)

    # Per-class and Macro-F1
    per_class: Dict[str, Dict[str, float]] = {}
    f1_list: List[float] = []
    for intent_name in taxonomy_intents:
        tp = sum(
            1
            for c, t in intent_eligible
            if c.expected.intent == intent_name
            and t.get("predicted", {}).get("intent") == intent_name
        )
        fp = sum(
            1
            for c, t in intent_eligible
            if c.expected.intent != intent_name
            and t.get("predicted", {}).get("intent") == intent_name
        )
        fn = sum(
            1
            for c, t in intent_eligible
            if c.expected.intent == intent_name
            and t.get("predicted", {}).get("intent") != intent_name
        )
        supp = sum(1 for c, _ in intent_eligible if c.expected.intent == intent_name)

        prec = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        rec = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        per_class[intent_name] = {
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "support": supp,
        }
        if supp > 0 or fp > 0:
            f1_list.append(f1)

    macro_f1 = (sum(f1_list) / len(f1_list)) if f1_list else 0.0

    # 2. Decision metrics
    decision_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if not is_verified_view or "decision" in c.verified_fields:
            decision_eligible.append((c, t))

    dec_correct = sum(
        1
        for c, t in decision_eligible
        if t.get("predicted", {}).get("decision") == c.expected.decision
    )
    n_dec = len(decision_eligible)
    dec_acc = (dec_correct / n_dec) if n_dec > 0 else 0.0

    # 3. Escalation metrics
    esc_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if not is_verified_view or "escalate" in c.verified_fields:
            esc_eligible.append((c, t))

    tp_esc = sum(
        1
        for c, t in esc_eligible
        if c.expected.escalate and bool(t.get("predicted", {}).get("escalate"))
    )
    fp_esc = sum(
        1
        for c, t in esc_eligible
        if not c.expected.escalate and bool(t.get("predicted", {}).get("escalate"))
    )
    fn_esc = sum(
        1
        for c, t in esc_eligible
        if c.expected.escalate and not bool(t.get("predicted", {}).get("escalate"))
    )
    tn_esc = sum(
        1
        for c, t in esc_eligible
        if not c.expected.escalate and not bool(t.get("predicted", {}).get("escalate"))
    )

    esc_prec = (tp_esc / (tp_esc + fp_esc)) if (tp_esc + fp_esc) > 0 else 0.0
    esc_rec = (tp_esc / (tp_esc + fn_esc)) if (tp_esc + fn_esc) > 0 else 0.0
    esc_f1 = (2.0 * esc_prec * esc_rec / (esc_prec + esc_rec)) if (esc_prec + esc_rec) > 0 else 0.0
    missed_esc_rate = (fn_esc / (tp_esc + fn_esc)) if (tp_esc + fn_esc) > 0 else 0.0
    false_esc_rate = (fp_esc / (tn_esc + fp_esc)) if (tn_esc + fp_esc) > 0 else 0.0

    # 4. Queue metrics
    queue_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if not is_verified_view or "queue" in c.verified_fields:
            queue_eligible.append((c, t))

    n_q = len(queue_eligible)
    q_correct = sum(
        1
        for c, t in queue_eligible
        if t.get("predicted", {}).get("queue") == c.expected.queue
    )
    queue_acc = (q_correct / n_q) if n_q > 0 else 0.0

    urgent_cases = [c for c, _ in queue_eligible if c.expected.queue == "human-urgent"]
    urgent_total = len(urgent_cases)
    urgent_passed = sum(
        1
        for c, t in queue_eligible
        if c.expected.queue == "human-urgent"
        and t.get("predicted", {}).get("queue") == "human-urgent"
    )
    urgent_recall = (urgent_passed / urgent_total) if urgent_total > 0 else 1.0

    # 5. Strict safety recall
    strict_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if is_verified_view:
            is_strict = (
                (c.expected.sentiment == "angry" and "sentiment" in c.verified_fields)
                or (_parse_urgency_to_int(c.expected.urgency) >= 3 and "urgency" in c.verified_fields)
                or (c.expected.queue == "human-urgent" and "queue" in c.verified_fields)
            )
            if is_strict and "escalate" in c.verified_fields:
                strict_eligible.append((c, t))
        else:
            is_strict = (
                c.expected.sentiment == "angry"
                or _parse_urgency_to_int(c.expected.urgency) >= 3
                or c.expected.queue == "human-urgent"
            )
            if is_strict:
                strict_eligible.append((c, t))

    strict_total = len(strict_eligible)
    strict_passed = sum(
        1 for c, t in strict_eligible if bool(t.get("predicted", {}).get("escalate"))
    )
    strict_recall = (strict_passed / strict_total) if strict_total > 0 else 1.0
    wilson_lb, _ = wilson_interval(strict_passed, strict_total)

    # 6. Routing Correctness
    # A case is correct iff:
    # decision is correct
    # AND (when auto_rag) predicted intent in acceptable_intents
    # AND (when escalate) predicted queue == expected queue
    routing_eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in eligible_pairs:
        if is_verified_view:
            if "decision" not in c.verified_fields:
                continue
            if c.expected.decision == "auto_rag" and "intent" not in c.verified_fields:
                continue
            if c.expected.decision == "escalate" and "queue" not in c.verified_fields:
                continue
        routing_eligible.append((c, t))

    def _is_routing_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
        pred = t.get("predicted", {})
        p_dec = pred.get("decision")
        if p_dec != c.expected.decision:
            return False
        if c.expected.decision == "auto_rag":
            return pred.get("intent") in c.expected.acceptable_intents
        if c.expected.decision == "escalate":
            return pred.get("queue") == c.expected.queue
        return False

    n_rc = len(routing_eligible)
    rc_passed = sum(1 for c, t in routing_eligible if _is_routing_correct(c, t))
    routing_correctness = (rc_passed / n_rc) if n_rc > 0 else 0.0

    # 7. System metrics
    tier_counts: Dict[str, int] = {}
    tier2_invocations = 0
    degraded_count = 0
    error_count = 0
    auto_res_count = 0
    valid_tax_count = 0

    for _, t in eligible_pairs:
        pred = t.get("predicted", {})
        tier = pred.get("tier", "unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        tier_calls = t.get("tier_calls", [])
        invoked_t2 = (tier == "tier2_flash") or any(
            call.get("tier") == "tier2_flash" for call in tier_calls
        )
        if invoked_t2:
            tier2_invocations += 1

        if bool(pred.get("degraded", False)) or tier == "fallback_degraded":
            degraded_count += 1

        if t.get("error") is not None or tier == "error":
            error_count += 1

        if pred.get("decision") == "auto_rag":
            auto_res_count += 1

        if pred.get("intent") in taxonomy_intents:
            valid_tax_count += 1

    tier_share = {tier: round(cnt / n_cases, 4) for tier, cnt in sorted(tier_counts.items())}
    tier2_rate = tier2_invocations / n_cases
    degraded_rate = degraded_count / n_cases
    error_rate = error_count / n_cases
    auto_res_share = auto_res_count / n_cases
    tax_validity = valid_tax_count / n_cases

    # 8. Bootstrap Confidence Intervals on headline metrics
    # Re-sample groups for routing_eligible items
    ci_results: Dict[str, List[float]] = {
        "routing_correctness": [0.0, 0.0],
        "intent_accuracy": [0.0, 0.0],
        "decision_accuracy": [0.0, 0.0],
        "strict_safety_recall": [0.0, 0.0],
        "macro_f1": [0.0, 0.0],
    }

    if routing_eligible:
        rc_groups = [c.group_id for c, _ in routing_eligible]

        def _metric_rc(sample: List[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
            if not sample:
                return 0.0
            return sum(1 for c, t in sample if _is_routing_correct(c, t)) / len(sample)

        rc_low, rc_high = bootstrap_ci(
            _metric_rc, routing_eligible, rc_groups, n_resamples=n_bootstrap, seed=seed
        )
        ci_results["routing_correctness"] = [round(rc_low, 4), round(rc_high, 4)]

    if intent_eligible:
        intent_groups = [c.group_id for c, _ in intent_eligible]

        def _metric_intent(sample: List[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
            if not sample:
                return 0.0
            return sum(
                1 for c, t in sample if t.get("predicted", {}).get("intent") == c.expected.intent
            ) / len(sample)

        i_low, i_high = bootstrap_ci(
            _metric_intent, intent_eligible, intent_groups, n_resamples=n_bootstrap, seed=seed
        )
        ci_results["intent_accuracy"] = [round(i_low, 4), round(i_high, 4)]

        def _metric_macro_f1(sample: List[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
            if not sample:
                return 0.0
            f1s: List[float] = []
            for iname in taxonomy_intents:
                tp = sum(
                    1
                    for c, t in sample
                    if c.expected.intent == iname
                    and t.get("predicted", {}).get("intent") == iname
                )
                fp = sum(
                    1
                    for c, t in sample
                    if c.expected.intent != iname
                    and t.get("predicted", {}).get("intent") == iname
                )
                fn = sum(
                    1
                    for c, t in sample
                    if c.expected.intent == iname
                    and t.get("predicted", {}).get("intent") != iname
                )
                supp = sum(1 for c, _ in sample if c.expected.intent == iname)
                if supp > 0 or fp > 0:
                    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
                    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
                    f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
                    f1s.append(f1)
            return (sum(f1s) / len(f1s)) if f1s else 0.0

        mf1_low, mf1_high = bootstrap_ci(
            _metric_macro_f1, intent_eligible, intent_groups, n_resamples=n_bootstrap, seed=seed
        )
        ci_results["macro_f1"] = [round(mf1_low, 4), round(mf1_high, 4)]

    if decision_eligible:
        dec_groups = [c.group_id for c, _ in decision_eligible]

        def _metric_dec(sample: List[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
            if not sample:
                return 0.0
            return sum(
                1 for c, t in sample if t.get("predicted", {}).get("decision") == c.expected.decision
            ) / len(sample)

        d_low, d_high = bootstrap_ci(
            _metric_dec, decision_eligible, dec_groups, n_resamples=n_bootstrap, seed=seed
        )
        ci_results["decision_accuracy"] = [round(d_low, 4), round(d_high, 4)]

    if strict_eligible:
        strict_groups = [c.group_id for c, _ in strict_eligible]

        def _metric_strict(sample: List[Tuple[RoutingCase, Dict[str, Any]]]) -> float:
            if not sample:
                return 1.0
            return sum(1 for _, t in sample if bool(t.get("predicted", {}).get("escalate"))) / len(
                sample
            )

        s_low, s_high = bootstrap_ci(
            _metric_strict, strict_eligible, strict_groups, n_resamples=n_bootstrap, seed=seed
        )
        ci_results["strict_safety_recall"] = [round(s_low, 4), round(s_high, 4)]

    return {
        "count": n_cases,
        "routing_correctness": round(routing_correctness, 4),
        "ci_95": ci_results,
        "intent_accuracy": round(intent_acc, 4),
        "lenient_accuracy": round(lenient_acc, 4),
        "macro_f1": round(macro_f1, 4),
        "per_class": per_class,
        "confusion_matrix": confusion,
        "taxonomy_validity_rate": round(tax_validity, 4),
        "decision_accuracy": round(dec_acc, 4),
        "escalation_precision": round(esc_prec, 4),
        "escalation_recall": round(esc_rec, 4),
        "escalation_f1": round(esc_f1, 4),
        "missed_escalation_rate": round(missed_esc_rate, 4),
        "false_escalation_rate": round(false_esc_rate, 4),
        "queue_accuracy": round(queue_acc, 4),
        "urgent_queue_recall": round(urgent_recall, 4),
        "strict_safety_recall": round(strict_recall, 4),
        "strict_safety_wilson_lb": round(wilson_lb, 4),
        "strict_safety_total": strict_total,
        "strict_safety_passed": strict_passed,
        "tier_share": tier_share,
        "tier2_invocation_rate": round(tier2_rate, 4),
        "degraded_rate": round(degraded_rate, 4),
        "error_rate": round(error_rate, 4),
        "auto_resolution_share": round(auto_res_share, 4),
        "ece": round(ece, 4),
    }


def _compute_slices(
    matched_pairs: Sequence[Tuple[RoutingCase, Dict[str, Any]]],
    taxonomy_intents: Sequence[str],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Compute slice summaries for all required dimensions."""

    def _is_routing_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
        pred = t.get("predicted", {})
        if pred.get("decision") != c.expected.decision:
            return False
        if c.expected.decision == "auto_rag":
            return pred.get("intent") in c.expected.acceptable_intents
        if c.expected.decision == "escalate":
            return pred.get("queue") == c.expected.queue
        return False

    def _summarize_slice(pairs: List[Tuple[RoutingCase, Dict[str, Any]]]) -> Dict[str, Any]:
        n = len(pairs)
        if n == 0:
            return {"count": 0, "routing_correctness": 0.0, "intent_acc": 0.0, "dec_acc": 0.0}
        rc = sum(1 for c, t in pairs if _is_routing_correct(c, t)) / n
        i_acc = (
            sum(1 for c, t in pairs if t.get("predicted", {}).get("intent") == c.expected.intent) / n
        )
        d_acc = (
            sum(
                1
                for c, t in pairs
                if t.get("predicted", {}).get("decision") == c.expected.decision
            )
            / n
        )
        return {
            "count": n,
            "routing_correctness": round(rc, 4),
            "intent_accuracy": round(i_acc, 4),
            "decision_accuracy": round(d_acc, 4),
        }

    slices: Dict[str, Dict[str, Dict[str, Any]]] = {
        "split": {},
        "source": {},
        "scenario": {},
        "intent": {},
        "difficulty": {},
        "register": {},
        "length_bucket": {},
        "label_status": {},
        "deciding_tier": {},
    }

    # Group by attributes
    for c, t in matched_pairs:
        pred = t.get("predicted", {})
        dec_tier = str(pred.get("tier", "unknown"))

        # split
        slices["split"].setdefault(c.split, []).append((c, t))
        # source
        slices["source"].setdefault(c.source, []).append((c, t))
        # scenario (multi-label)
        for sc in c.scenarios:
            slices["scenario"].setdefault(sc, []).append((c, t))
        # intent
        slices["intent"].setdefault(c.expected.intent, []).append((c, t))
        # difficulty
        slices["difficulty"].setdefault(c.difficulty, []).append((c, t))
        # register
        slices["register"].setdefault(c.style.register, []).append((c, t))
        # length_bucket
        slices["length_bucket"].setdefault(c.style.length_bucket, []).append((c, t))
        # label_status
        slices["label_status"].setdefault(c.label_status, []).append((c, t))
        # deciding_tier
        slices["deciding_tier"].setdefault(dec_tier, []).append((c, t))

    # Convert grouped lists to summary dicts
    out_slices: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for dim, group_dict in slices.items():
        out_slices[dim] = {val: _summarize_slice(pairs) for val, pairs in sorted(group_dict.items())}

    return out_slices


def compute_objective(
    matched_pairs: Sequence[Tuple[RoutingCase, Dict[str, Any]]],
    is_verified_view: bool = False,
) -> Tuple[float, Dict[str, float]]:
    """Compute the optimization objective: routing correctness macro-averaged over scenarios."""

    def _is_routing_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
        pred = t.get("predicted", {})
        if pred.get("decision") != c.expected.decision:
            return False
        if c.expected.decision == "auto_rag":
            return pred.get("intent") in c.expected.acceptable_intents
        if c.expected.decision == "escalate":
            return pred.get("queue") == c.expected.queue
        return False

    eligible: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for c, t in matched_pairs:
        if c.label_status == "rejected":
            continue
        if is_verified_view:
            if c.label_status != "verified":
                continue
            if "decision" not in c.verified_fields:
                continue
            if c.expected.decision == "auto_rag" and "intent" not in c.verified_fields:
                continue
            if c.expected.decision == "escalate" and "queue" not in c.verified_fields:
                continue
        eligible.append((c, t))

    if not eligible:
        return (0.0, {})

    scenario_map: Dict[str, List[Tuple[RoutingCase, Dict[str, Any]]]] = {}
    for c, t in eligible:
        for sc in c.scenarios:
            scenario_map.setdefault(sc, []).append((c, t))

    scenario_scores: Dict[str, float] = {}
    for sc, sc_pairs in sorted(scenario_map.items()):
        if sc_pairs:
            correct = sum(1 for c, t in sc_pairs if _is_routing_correct(c, t))
            scenario_scores[sc] = round(correct / len(sc_pairs), 4)

    if not scenario_scores:
        return (0.0, {})

    macro_objective = sum(scenario_scores.values()) / len(scenario_scores)
    return (round(macro_objective, 4), scenario_scores)


def compute(
    traces: Sequence[Dict[str, Any]],
    cases: Sequence[RoutingCase],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute complete deterministic evaluation metrics over traces and cases.

    Args:
        traces: Sequence of router trace dictionaries.
        cases: Sequence of RoutingCase instances.
        cfg: Optional evaluation configuration dictionary.

    Returns:
        Structured evaluation scorecard containing verified and all views,
        slices, calibration, and objective scores.

    Raises:
        ValueError: If any trace is marked as dry-run.
    """
    cfg = cfg or {}
    seed = int(cfg.get("seed", 20261028))
    n_bootstrap = int(cfg.get("bootstrap_resamples", 1000))

    # Reject dry-run traces immediately
    for t in traces:
        if t.get("is_dry_run") is True:
            raise ValueError(
                f"Dry-run trace detected for ticket_id='{t.get('ticket_id')}'. "
                "Dry-run traces cannot be scored for production metrics."
            )

    tax = load_taxonomy()
    taxonomy_intents = list(tax.intents)

    case_map = {c.ticket_id: c for c in cases}

    # Match traces and cases, dropping rejected ground truth cases
    matched_pairs: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for t in traces:
        tid = t.get("ticket_id")
        if tid in case_map:
            c = case_map[tid]
            if c.label_status != "rejected":
                matched_pairs.append((c, t))

    verified_metrics = _compute_view_metrics(
        matched_pairs, taxonomy_intents, is_verified_view=True, seed=seed, n_bootstrap=n_bootstrap
    )
    all_metrics = _compute_view_metrics(
        matched_pairs, taxonomy_intents, is_verified_view=False, seed=seed, n_bootstrap=n_bootstrap
    )

    obj_verified, sc_verified = compute_objective(matched_pairs, is_verified_view=True)
    obj_all, sc_all = compute_objective(matched_pairs, is_verified_view=False)

    slices = _compute_slices(matched_pairs, taxonomy_intents)

    return {
        "total_cases": len(cases),
        "evaluated_cases": len(matched_pairs),
        "verified": verified_metrics,
        "all": all_metrics,
        "objective": {
            "verified": obj_verified,
            "all": obj_all,
            "scenario_strata_all": sc_all,
            "scenario_strata_verified": sc_verified,
        },
        "slices": slices,
    }
