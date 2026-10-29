"""Command line interface for the live per-path latency benchmark.

# REQ-EVAL-011: Live Per-Path Latency Benchmark with Trace-Level Step Attribution
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from evals.latency.bench import (
    DEFAULT_ENGINE_ID,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_PROJECT_NUMBER,
    DEFAULT_REGION,
    CachedTokenProvider,
    plan_requests,
    run_bench,
)
from evals.latency.correlate import DEFAULT_PROJECT_ID, correlate_records
from evals.latency.report import (
    compute_summary,
    get_next_run_dir,
    write_report_artifacts,
)


def load_cases(cases_path: Path) -> list[dict]:
    """Load JSONL test cases."""
    if not cases_path.exists():
        raise FileNotFoundError(f"Cases file not found: {cases_path}")
    cases = []
    with open(cases_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def build_parser() -> argparse.ArgumentParser:
    """Build command line argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.latency",
        description="Live per-path latency benchmark with trace-level step attribution.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: run
    run_p = subparsers.add_parser("run", help="Execute live latency benchmark and generate report.")
    run_p.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).parent / "cases_v1.jsonl",
        help="Path to cases_v1.jsonl file.",
    )
    run_p.add_argument("--reps", type=int, default=5, help="Number of repetitions per case.")
    run_p.add_argument("--paths", type=str, default=None, help="Comma-separated path filters.")
    run_p.add_argument("--case", type=str, default=None, help="Substring filter on case_id or message.")
    run_p.add_argument("--label", type=str, default=None, help="Optional label suffix for run directory.")
    run_p.add_argument(
        "--spacing-seconds",
        type=float,
        default=1.0,
        help="Inter-request spacing in seconds.",
    )
    run_p.add_argument(
        "--settle-seconds",
        type=float,
        default=30.0,
        help="Wait time in seconds after last request before polling telemetry.",
    )
    run_p.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help="Safety cap on total requests in plan.",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan and cost estimate without making network calls.",
    )
    run_p.add_argument(
        "--out-root",
        type=Path,
        default=Path("evals/results/latency"),
        help="Root directory for benchmark run outputs.",
    )
    run_p.add_argument(
        "--engine-id",
        type=str,
        default=DEFAULT_ENGINE_ID,
        help="Vertex AI Reasoning Engine ID.",
    )
    run_p.add_argument(
        "--project-number",
        type=str,
        default=DEFAULT_PROJECT_NUMBER,
        help="GCP Project numeric ID.",
    )
    run_p.add_argument(
        "--project-id",
        type=str,
        default=DEFAULT_PROJECT_ID,
        help="GCP Project string ID.",
    )
    run_p.add_argument(
        "--region",
        type=str,
        default=DEFAULT_REGION,
        help="GCP location/region.",
    )

    # Subcommand: report
    rep_p = subparsers.add_parser("report", help="Re-correlate missing traces and re-render report.")
    rep_p.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Path to existing benchmark run directory.",
    )
    rep_p.add_argument(
        "--project-id",
        type=str,
        default=DEFAULT_PROJECT_ID,
        help="GCP Project string ID.",
    )
    rep_p.add_argument(
        "--settle-seconds",
        type=float,
        default=0.0,
        help="Wait time before correlation.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for latency benchmark CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        cases = load_cases(args.cases)
        plan = plan_requests(
            cases=cases,
            reps=args.reps,
            paths=args.paths,
            case_filter=args.case,
        )

        if args.dry_run:
            run_bench(
                plan=plan,
                max_requests=args.max_requests,
                dry_run=True,
            )
            return 0

        if len(plan) > args.max_requests:
            print(
                f"Error: Planned {len(plan)} requests exceeds --max-requests cap "
                f"({args.max_requests}). Aborting.",
                file=sys.stderr,
            )
            return 1

        print(f"Starting latency benchmark: {len(plan)} requests across {len(cases)} cases...")
        import requests

        http_session = requests.Session()
        token_provider = CachedTokenProvider()

        raw_records = run_bench(
            plan=plan,
            engine_id=args.engine_id,
            project_number=args.project_number,
            region=args.region,
            token_provider=token_provider,
            http=http_session,
            spacing_seconds=args.spacing_seconds,
            max_requests=args.max_requests,
            dry_run=False,
        )

        print(f"Bench complete ({len(raw_records)} calls). Correlating logs and traces...")
        token = token_provider()
        correlated = correlate_records(
            records=raw_records,
            token=token,
            http=http_session,
            project_id=args.project_id,
            settle_seconds=args.settle_seconds,
        )

        summary = compute_summary(correlated)
        run_dir = get_next_run_dir(args.out_root, label=args.label)
        artifacts = write_report_artifacts(run_dir, correlated, summary)

        print("\nRun completed successfully!")
        print(f"Artifacts written to: {run_dir}")
        print(f"  - Requests: {artifacts['requests']}")
        print(f"  - Summary:  {artifacts['summary']}")
        print(f"  - Report:   {artifacts['report']}")
        return 0

    if args.command == "report":
        req_file = args.run_dir / "requests.jsonl"
        if not req_file.exists():
            print(f"Error: requests.jsonl not found in {args.run_dir}", file=sys.stderr)
            return 1

        records = []
        with open(req_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))

        print(f"Loaded {len(records)} records from {req_file}. Correlating telemetry...")
        import requests

        http_session = requests.Session()
        token_provider = CachedTokenProvider()
        token = token_provider()

        correlated = correlate_records(
            records=records,
            token=token,
            http=http_session,
            project_id=args.project_id,
            settle_seconds=args.settle_seconds,
        )

        summary = compute_summary(correlated)
        artifacts = write_report_artifacts(args.run_dir, correlated, summary)
        print(f"Re-correlation complete. Updated artifacts in {args.run_dir}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
