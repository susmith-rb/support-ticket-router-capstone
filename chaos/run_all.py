"""CLI runner for resilience and chaos engineering experiments.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration

Usage:
    # Run all experiments in safe dry-run mode (default):
    python3 chaos/run_all.py

    # Run specific experiment in dry-run mode:
    python3 chaos/run_all.py --experiment router_s1
    python3 chaos/run_all.py --experiment gemini_429
    python3 chaos/run_all.py --experiment cloud_sql

    # Live execution against Google Cloud (requires both flags):
    python3 chaos/run_all.py --experiment router_s1 --execute --confirm-destructive
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chaos.base import ChaosSafetyError
from chaos.experiment_cloud_sql import CloudSqlChaosExperiment
from chaos.experiment_gemini_429 import Gemini429ChaosExperiment
from chaos.experiment_router_s1 import RouterS1ChaosExperiment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("chaos.runner")


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chaos Engineering Experiment Runner")
    parser.add_argument(
        "--experiment",
        choices=["router_s1", "gemini_429", "cloud_sql", "all"],
        default="all",
        help="Experiment to run: router_s1, gemini_429, cloud_sql, or all (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Execute in safe dry-run simulation mode (default: True)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Execute live against Google Cloud infrastructure",
    )
    parser.add_argument(
        "--confirm-destructive",
        action="store_true",
        default=False,
        help="Mandatory confirmation flag required alongside --execute to prevent unintended outages",
    )
    parsed = parser.parse_args(args)
    # If --execute is passed, dry_run becomes False
    if parsed.execute:
        parsed.dry_run = False
    return parsed


def main() -> None:
    args = parse_args()
    logger.info("Initializing chaos runner: experiment=%s, dry_run=%s, execute=%s", args.experiment, args.dry_run, args.execute)

    experiments = []
    if args.experiment in ("router_s1", "all"):
        experiments.append(
            RouterS1ChaosExperiment(
                dry_run=args.dry_run,
                execute=args.execute,
                confirm_destructive=args.confirm_destructive,
            )
        )
    if args.experiment in ("gemini_429", "all"):
        experiments.append(
            Gemini429ChaosExperiment(
                dry_run=args.dry_run,
                execute=args.execute,
                confirm_destructive=args.confirm_destructive,
            )
        )
    if args.experiment in ("cloud_sql", "all"):
        experiments.append(
            CloudSqlChaosExperiment(
                dry_run=args.dry_run,
                execute=args.execute,
                confirm_destructive=args.confirm_destructive,
            )
        )

    results = []
    failed_count = 0

    for exp in experiments:
        logger.info("\n" + "=" * 60)
        logger.info("RUNNING CHAOS EXPERIMENT: %s", exp.name)
        logger.info("=" * 60)
        try:
            report = exp.run()
            results.append(report)
            assertion = report.get("phases", {}).get("assertion", {})
            if not assertion.get("passed"):
                failed_count += 1
        except ChaosSafetyError as sec_err:
            logger.error("Chaos safety gate rejected execution: %s", sec_err)
            sys.exit(2)
        except Exception as exc:
            logger.critical("Chaos experiment '%s' crashed with unhandled exception: %s", exp.name, exc)
            failed_count += 1

    print("\n" + "=" * 60)
    print("CHAOS RUN SUMMARY")
    print(f"Total Experiments:   {len(experiments)}")
    print(f"Passed Assertions:   {len(experiments) - failed_count}")
    print(f"Failed Assertions:   {failed_count}")
    for res in results:
        name = res.get("experiment")
        status = res.get("status")
        assertion = res.get("phases", {}).get("assertion", {})
        passed = assertion.get("passed", False)
        print(f"  - {name:<22}: status={status}, assertion_passed={passed}")
    print("=" * 60 + "\n")

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
