"""Command-line interface for routing evaluation runs.

# REQ-REVAL-011: Runner CLI targets, replay/policy/deployed, dry-run, cost cap.
# REQ-REVAL-010: Held-out pre-registration & single use guard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from evals.routing.runner import compute_config_hash, load_candidate_overlay, run
from evals.routing.schema import read_jsonl
from ticket_router.config.agent_tuning import get_tuning_config
from ticket_router.config.models import model_id

logger = logging.getLogger("evals.routing.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for routing eval run command."""
    parser = argparse.ArgumentParser(
        prog="evals.routing run",
        description="Run routing cascade evaluation against dev, regression, or heldout datasets.",
    )
    parser.add_argument(
        "--split",
        default="dev",
        choices=["dev", "regression", "heldout"],
        help="Dataset split to evaluate (default: dev)",
    )
    parser.add_argument(
        "--dataset",
        default="evals/data/routing_v1",
        help="Path to evaluation dataset directory containing JSONL splits and manifest (default: evals/data/routing_v1)",
    )
    parser.add_argument(
        "--target",
        default="policy",
        choices=["policy", "replay", "deployed"],
        help="Runner execution target (default: policy)",
    )
    parser.add_argument(
        "--candidate",
        default="baseline",
        help="Candidate configuration overlay (default: baseline)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without network requests, render prompts, and estimate cost",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=5.0,
        help="Maximum allowable live execution cost before aborting (default: 5.00)",
    )
    parser.add_argument(
        "--reliability-k",
        type=int,
        default=1,
        help="Number of stochastic passes to collect for Tier-2-decided cases (default: 1)",
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Allow live tier calls on cache misses during replay runs",
    )
    parser.add_argument(
        "--with-model-armor",
        action="store_true",
        help="Enable Model Armor sanitization tier",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Custom label suffix for the generated run directory (run_NNN_<label>)",
    )
    parser.add_argument(
        "--preregistration",
        default=None,
        help="Mandatory pre-registration ID for heldout split evaluation",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Maximum thread worker concurrency (default: 8)",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force live calls even on cache hits and overwrite cached entries for clean latency measurement",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Explicit run directory path (if omitted, next_run_dir is used)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for routing eval run subcommand."""
    raw_args = list(argv if argv is not None else sys.argv[1:])
    if raw_args and raw_args[0] == "run":
        raw_args = raw_args[1:]

    parser = build_parser()
    args = parser.parse_args(raw_args)

    # -------------------------------------------------------------------------
    # REQ-REVAL-010: Held-Out Pre-Registration & Single-Use Guard
    # -------------------------------------------------------------------------
    if args.split == "heldout" and not args.preregistration:
        raise ValueError(
            "--preregistration ID is required when running on the heldout split. "
            "Register candidate configuration first using evals.routing.heldout.preregister."
        )

    from evals.routing.report import resolve_dataset_path

    resolved_dataset = resolve_dataset_path(args.dataset, split=args.split)
    if resolved_dataset.is_file():
        split_file = resolved_dataset
        dataset_dir = resolved_dataset.parent
    else:
        dataset_dir = resolved_dataset
        split_file = dataset_dir / f"{args.split}.jsonl"

    if not split_file.is_file():
        raise FileNotFoundError(f"Evaluation dataset split file not found at: {split_file}")

    cases = read_jsonl(split_file)
    manifest_file = dataset_dir / "manifest.json"
    manifest_sha256 = None
    manifest_data: Dict[str, Any] = {}
    if manifest_file.is_file():
        manifest_bytes = manifest_file.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_data = json.loads(manifest_bytes.decode("utf-8"))

    if args.split == "heldout":
        # Scan completed dev runs to verify candidate was tested on dev first
        results_root = Path("evals/results/routing")
        completed_dev_hashes: set[str] = set()
        if results_root.is_dir():
            for run_p in results_root.glob("run_*"):
                snap_file = run_p / "config_snapshot.json"
                if snap_file.is_file():
                    try:
                        snap_data = json.loads(snap_file.read_text(encoding="utf-8"))
                        run_args = snap_data.get("args", {})
                        if run_args.get("split") == "dev" or "dev" in run_p.name:
                            ch = snap_data.get("config_hash")
                            if ch:
                                completed_dev_hashes.add(ch)
                    except Exception:
                        pass

        cand_name, overlay_dict = load_candidate_overlay(args.candidate)
        merged_tuning = get_tuning_config(reload=True, overlay=overlay_dict)
        m_ids = {
            "router_tier2": model_id("router_tier2"),
            "router_s1": "router-s1",
        }
        p_versions = {
            "tier2_router_version": merged_tuning.tier2_router_version,
            "faq_answer_version": merged_tuning.faq_answer_version,
        }
        cfg_hash = compute_config_hash(merged_tuning, m_ids, p_versions)

        from evals.routing.heldout import check_allowed

        check_allowed(
            prereg_id=args.preregistration,
            dataset_version=manifest_data.get("dataset_version", "routing_v1"),
            manifest_sha=manifest_sha256 or manifest_data.get("manifest_sha256", ""),
            config_hash=cfg_hash,
            completed_dev_config_hashes=completed_dev_hashes,
        )

    # Determine execution mode
    if args.target == "replay":
        eff_mode = "replay_or_live" if args.allow_live else "replay"
    elif args.target == "policy":
        # Reuse cached tier outputs by default so baseline and candidate share identical Tier 1 results
        # (paired comparison); --refresh-cache forces fresh live calls (e.g. for latency measurement).
        eff_mode = "live" if args.refresh_cache else "replay_or_live"
    else:
        eff_mode = "live"

    traces = run(
        cases=cases,
        target=args.target,
        candidate_overlay=args.candidate,
        run_dir=args.run_dir,
        mode=eff_mode,
        max_cost_usd=args.max_cost_usd,
        concurrency=args.concurrency,
        reliability_k=args.reliability_k,
        with_model_armor=args.with_model_armor,
        dry_run=args.dry_run,
        refresh_cache=args.refresh_cache,
        label=args.label or f"{args.split}_{args.candidate}",
        manifest_sha256=manifest_sha256,
    )

    # If heldout completed and not dry run, record result
    if args.split == "heldout" and not args.dry_run and args.preregistration:
        from evals.routing.heldout import record_result

        record_result(
            prereg_id=args.preregistration,
            results_summary={
                "run_id": traces[0].run_id if traces else "empty",
                "cases_count": len(traces),
                "cost_usd_total": sum(t.cost_usd_total for t in traces),
            },
        )

    # One-line summary: (n cases, tier share, cost, errors, run dir)
    n_cases = len(traces)
    tier_counts: Dict[str, int] = {}
    err_count = 0
    for t in traces:
        if t.error:
            err_count += 1
        tr = t.predicted.get("tier", "unknown")
        tier_counts[tr] = tier_counts.get(tr, 0) + 1

    t0_count = tier_counts.get("tier0_rules", 0)
    t1_count = tier_counts.get("tier1_router_s1", 0)
    t2_count = tier_counts.get("tier2_flash", 0)

    t0_pct = (t0_count / n_cases * 100.0) if n_cases > 0 else 0.0
    t1_pct = (t1_count / n_cases * 100.0) if n_cases > 0 else 0.0
    t2_pct = (t2_count / n_cases * 100.0) if n_cases > 0 else 0.0
    total_cost = sum(t.cost_usd_total for t in traces)
    run_dir_out = traces[0].run_id if traces else "none"

    print(
        f"Summary: {n_cases} cases | Tier 0: {t0_pct:.1f}%, Tier 1: {t1_pct:.1f}%, Tier 2: {t2_pct:.1f}% | "
        f"Cost: ${total_cost:.4f} | Errors: {err_count} | Run dir: {run_dir_out}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
