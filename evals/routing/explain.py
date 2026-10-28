"""Inspection and diagnostic CLI for full routing tier trails.

# REQ-REVAL-017: Failure clusters, representative errors, explain CLI.
Prints complete tier trails for evaluated cases:
- Case text, ground truth expected labels, and provenance
- Router predicted intent, decision, queue, and escalation status
- Tier call audit log (Tier 0 rules, Tier 1 probabilities, Tier 2 raw prompts & responses)
Filterable by cluster ID, individual ticket ID, or dataset slice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from evals.routing.schema import RoutingCase, read_jsonl


def format_case_trail(case: RoutingCase, trace: Dict[str, Any]) -> str:
    """Format a case and its full router execution trace into a human-readable string."""
    pred = trace.get("predicted", {})
    exp = case.expected
    tier_calls = trace.get("tier_calls", [])
    raw_dec = trace.get("decision_raw", {})

    lines: List[str] = [
        "=" * 80,
        f"Ticket ID:   {case.ticket_id}",
        f"Split:       {case.split} | Scenarios: {', '.join(case.scenarios)} | Difficulty: {case.difficulty}",
        f"Provenance:  {case.source} ({case.ground_truth_source}) | Status: {case.label_status}",
        f"Style:       {case.style.register} | {case.style.length_bucket}",
        "-" * 80,
        f"Subject:     {case.subject or '(none)'}",
        "Description:",
        f"  {case.description}",
        "-" * 80,
        "EXPECTED LABELS:",
        f"  Intent:    {exp.intent} (acceptable: {list(exp.acceptable_intents)})",
        f"  Decision:  {exp.decision} | Escalate: {exp.escalate} | Queue: {exp.queue}",
        f"  Sentiment: {exp.sentiment} | Urgency: {exp.urgency}",
        "",
        "PREDICTED LABELS:",
        f"  Intent:    {pred.get('intent')} | Deciding Tier: {pred.get('tier')}",
        f"  Decision:  {pred.get('decision')} | Escalate: {pred.get('escalate')} | Queue: {pred.get('queue')}",
        f"  Sentiment: {pred.get('sentiment')} | Urgency: {pred.get('urgency')}",
        f"  Degraded:  {pred.get('degraded')} | Reason: {pred.get('reason')}",
        "-" * 80,
        f"TIER EXECUTION TRAIL ({len(tier_calls)} calls, Total Latency: {trace.get('latency_ms_total', 0.0):.1f}ms):",
    ]

    for idx, call in enumerate(tier_calls, 1):
        tier_name = call.get("tier", "unknown")
        model = call.get("model_id") or call.get("model_role") or "n/a"
        lat = call.get("latency_ms", 0.0)
        cost = call.get("cost_usd", 0.0)
        err = call.get("error")
        lines.append(f"  [{idx}] {tier_name} (model: {model}) — {lat:.1f}ms — ${cost:.5f}")
        if err:
            lines.append(f"      ERROR: {err}")
        if call.get("prediction"):
            lines.append(f"      Prediction: {call.get('prediction')}")

    if "details" in raw_dec:
        lines.append(f"Decision Details: {raw_dec['details']}")

    if trace.get("reliability"):
        rel_passes = trace["reliability"].get("passes", [])
        lines.append(f"Reliability ({len(rel_passes)} stochastic passes):")
        for p_idx, p in enumerate(rel_passes):
            lines.append(f"  Pass {p_idx}: intent={p.get('intent')} dec={p.get('decision')} esc={p.get('escalate')}")

    lines.append("=" * 80)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for explaining evaluation cases and failure clusters."""
    parser = argparse.ArgumentParser(description="Explain routing evaluation cases and failure clusters.")
    parser.add_argument("--run", required=True, help="Path to evaluation run directory.")
    parser.add_argument("--cluster", type=int, help="Cluster ID to inspect (from scorecard).")
    parser.add_argument("--ticket", help="Specific ticket_id to inspect.")
    parser.add_argument("--slice", help="Filter by slice attribute (e.g. intent=order_status, scenario=ambiguous).")
    parser.add_argument("--dataset", help="Optional override path to dataset JSONL.")
    parser.add_argument("--max", type=int, default=10, help="Maximum number of cases to print (default 10).")

    args = parser.parse_args(argv)
    run_dir = Path(args.run)

    if not run_dir.exists():
        print(f"Error: Run directory does not exist: {run_dir}", file=sys.stderr)
        return 1

    # Locate traces
    traces_path = run_dir / "traces.jsonl"
    if not traces_path.exists():
        snap_path = run_dir / "config_snapshot.json"
        if snap_path.exists():
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                run_id = snap.get("run_id")
                cached_traces = Path(".cache/routing_eval/runs") / str(run_id) / "traces.jsonl"
                if cached_traces.exists():
                    traces_path = cached_traces
            except Exception:
                pass

    if not traces_path.exists():
        print(f"Error: Could not locate traces.jsonl in {run_dir} or cache.", file=sys.stderr)
        return 1

    traces: List[Dict[str, Any]] = []
    with traces_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                traces.append(json.loads(line))

    # Locate dataset
    dataset_path: Optional[Path] = Path(args.dataset) if args.dataset else None
    if not dataset_path or not dataset_path.exists():
        snap_path = run_dir / "config_snapshot.json"
        if snap_path.exists():
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                if "dataset_path" in snap and Path(snap["dataset_path"]).exists():
                    dataset_path = Path(snap["dataset_path"])
            except Exception:
                pass

    if not dataset_path or not dataset_path.exists():
        # Fallback to dev dataset if present
        default_dev = Path("evals/data/routing_v1/dev.jsonl")
        if default_dev.exists():
            dataset_path = default_dev
        else:
            print("Error: Could not locate dataset JSONL. Specify via --dataset <path>.", file=sys.stderr)
            return 1

    cases = read_jsonl(dataset_path)
    case_map = {c.ticket_id: c for c in cases}
    trace_map = {t["ticket_id"]: t for t in traces if "ticket_id" in t}

    target_tids: Optional[List[str]] = None

    if args.ticket:
        target_tids = [args.ticket]
    elif args.cluster is not None:
        sc_path = run_dir / "scorecard.json"
        if not sc_path.exists():
            print(f"Error: scorecard.json not found in {run_dir}", file=sys.stderr)
            return 1
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
        clusters = sc.get("clusters", [])
        matched_cl = next((cl for cl in clusters if cl.get("cluster_id") == args.cluster), None)
        if not matched_cl:
            print(f"Error: Cluster {args.cluster} not found in scorecard.", file=sys.stderr)
            return 1
        print(f"--- Failure Cluster {args.cluster} ---")
        print(f"Error Type: {matched_cl.get('error_type')} | Transition: {matched_cl.get('transition')}")
        print(f"Size: {matched_cl.get('size')} | Score: {matched_cl.get('score')}")
        print(f"Signature terms: {', '.join(matched_cl.get('signature', []))}\n")
        target_tids = [rep["ticket_id"] for rep in matched_cl.get("representatives", [])]
    elif args.slice:
        if "=" not in args.slice:
            print("Error: --slice must be in key=value format (e.g. intent=order_status).", file=sys.stderr)
            return 1
        s_key, s_val = args.slice.split("=", 1)
        s_key = s_key.strip()
        s_val = s_val.strip()

        matched_tids: List[str] = []
        for tid, c in case_map.items():
            t = trace_map.get(tid, {})
            c_val = getattr(c, s_key, None)
            if c_val == s_val:
                matched_tids.append(tid)
            elif s_key in ("scenario", "scenarios") and s_val in c.scenarios:
                matched_tids.append(tid)
            elif s_key in ("intent", "decision", "queue") and getattr(c.expected, s_key, None) == s_val:
                matched_tids.append(tid)
            elif t.get("predicted", {}).get(s_key) == s_val:
                matched_tids.append(tid)
        target_tids = matched_tids
    else:
        target_tids = list(case_map.keys())

    printed = 0
    for tid in target_tids:
        if printed >= args.max:
            break
        if tid in case_map and tid in trace_map:
            print(format_case_trail(case_map[tid], trace_map[tid]))
            printed += 1

    if printed == 0:
        print("No matching cases found.", file=sys.stderr)
    else:
        print(f"\nDisplayed {printed} case(s).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
