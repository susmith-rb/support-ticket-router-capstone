"""Main CLI dispatcher for the independent routing evaluation harness.

# REQ-REVAL-012 / REQ-REVAL-013 / REQ-REVAL-016 / REQ-REVAL-017 / REQ-REVAL-019 / REQ-REVAL-021
Dispatches subcommands:
- data: Dataset ingestion and synthesis (lazy loads evals.routing.cli_data)
- run: Target execution and replay (lazy loads evals.routing.cli_run)
- report: Generates scorecard and markdown reports for an evaluation run
- compare: Paired comparison of baseline vs candidate evaluation runs
- explain: Tier trail inspection for cases and failure clusters
- preregister: Pre-registers held-out dataset evaluation configs
- smoke: Runs the hermetic CI smoke test suite
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def _load_yaml(path: Path | str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    import yaml

    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def handle_smoke(args: argparse.Namespace) -> int:
    del args
    from evals.routing.smoke import main as smoke_main

    return smoke_main()


def handle_explain(args: argparse.Namespace, rest: Sequence[str]) -> int:
    del args
    from evals.routing.explain import main as explain_main

    return explain_main(rest)


def handle_preregister(args: argparse.Namespace) -> int:
    from evals.routing.heldout import DEFAULT_REGISTRY_LOG, preregister

    cfg = _load_yaml(args.config) if args.config else {}
    gate_thresholds = cfg.get("gate_thresholds", {})
    if not gate_thresholds:
        print("Error: Configuration file must contain 'gate_thresholds'.", file=sys.stderr)
        return 1

    log_path = Path(args.log) if args.log else DEFAULT_REGISTRY_LOG
    reg_id = preregister(
        dataset_version=args.dataset_version,
        manifest_sha256=args.manifest_sha256,
        config_hash=args.config_hash,
        gate_thresholds=gate_thresholds,
        operator=args.operator,
        log_path=log_path,
    )
    print(f"Successfully pre-registered held-out evaluation: registration_id={reg_id}")
    return 0


def handle_report(args: argparse.Namespace) -> int:
    from evals.routing.report import resolve_dataset_path, write_run_artifacts
    from evals.routing.schema import read_jsonl

    run_dir = Path(args.run)
    if not run_dir.exists():
        print(f"Error: Run directory does not exist: {run_dir}", file=sys.stderr)
        return 1

    traces_path = run_dir / "traces.jsonl"
    if not traces_path.exists():
        print(f"Error: traces.jsonl not found in {run_dir}", file=sys.stderr)
        return 1

    traces: List[Dict[str, Any]] = []
    with traces_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                traces.append(json.loads(line))

    trace_split = traces[0].get("split") if traces else "dev"
    dataset_path = resolve_dataset_path(args.dataset, split=trace_split)
    if not dataset_path.exists():
        print(f"Error: Dataset file not found: {dataset_path}", file=sys.stderr)
        return 1
    cases = read_jsonl(dataset_path)

    cfg = _load_yaml(args.config) if args.config else _load_yaml("evals/routing/config.yaml")

    scorecard = write_run_artifacts(
        run_dir,
        traces,
        cases,
        cfg=cfg,
        baseline_run_dir=args.baseline,
        target=args.target,
    )
    print(f"Report generated successfully in {run_dir}")
    print(f"Overall gates passed: {scorecard.get('gates', {}).get('overall_passed')}")
    return 0


def handle_compare(args: argparse.Namespace) -> int:
    from evals.routing.compare import compare
    from evals.routing.report import resolve_dataset_path
    from evals.routing.schema import read_jsonl

    b_dir = Path(args.baseline)
    c_dir = Path(args.candidate)

    if not b_dir.exists() or not c_dir.exists():
        print("Error: Baseline and candidate directories must both exist.", file=sys.stderr)
        return 1

    b_sc_path = b_dir / "scorecard.json"
    c_sc_path = c_dir / "scorecard.json"
    b_traces_path = b_dir / "traces.jsonl"
    c_traces_path = c_dir / "traces.jsonl"

    for p in (b_sc_path, c_sc_path, b_traces_path, c_traces_path):
        if not p.exists():
            print(f"Error: Required file not found: {p}", file=sys.stderr)
            return 1

    b_sc = json.loads(b_sc_path.read_text(encoding="utf-8"))
    c_sc = json.loads(c_sc_path.read_text(encoding="utf-8"))

    b_traces = [json.loads(line) for line in b_traces_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    c_traces = [json.loads(line) for line in c_traces_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    trace_split = b_traces[0].get("split") if b_traces else "dev"
    dataset_path = resolve_dataset_path(args.dataset, split=trace_split)
    if not dataset_path.exists():
        print(f"Error: Dataset file not found: {dataset_path}", file=sys.stderr)
        return 1
    cases = read_jsonl(dataset_path)

    cfg = _load_yaml(args.config) if args.config else _load_yaml("evals/routing/config.yaml")

    res = compare(b_sc, b_traces, c_sc, c_traces, cases, cfg=cfg, log_path=args.log)
    print("=" * 60)
    print(f"VERDICT: {res.get('verdict')}")
    print(f"Wins: {res.get('wins_count')} | Losses: {res.get('losses_count')} | Ties: {res.get('ties_count')}")
    for r in res.get("reasons", []):
        print(f"- {r}")
    print("=" * 60)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI main dispatcher."""
    argv = list(argv if argv is not None else sys.argv[1:])

    if not argv:
        print("Usage: python -m evals.routing <data|run|report|compare|explain|preregister|smoke> [args]")
        return 0

    subcommand = argv[0]
    rest = argv[1:]

    # WP-B: data CLI lazy dispatch
    if subcommand == "data":
        try:
            from evals.routing.cli_data import main as cli_data_main

            return cli_data_main(rest)
        except ImportError as e:
            print(
                f"Command 'data' is not available because evals.routing.cli_data could not be loaded: {e}",
                file=sys.stderr,
            )
            return 1

    # WP-C: run CLI lazy dispatch
    if subcommand == "run":
        try:
            from evals.routing.cli_run import main as cli_run_main

            return cli_run_main(rest)
        except ImportError as e:
            print(
                f"Command 'run' is not available because evals.routing.cli_run could not be loaded: {e}",
                file=sys.stderr,
            )
            return 1

    if subcommand == "smoke":
        parser = argparse.ArgumentParser(prog="python -m evals.routing smoke")
        args = parser.parse_args(rest)
        return handle_smoke(args)

    if subcommand == "explain":
        return handle_explain(argparse.Namespace(), rest)

    if subcommand == "preregister":
        parser = argparse.ArgumentParser(prog="python -m evals.routing preregister")
        parser.add_argument("--dataset-version", required=True, help="Dataset semver version string")
        parser.add_argument("--manifest-sha256", required=True, help="Sha256 of manifest.json")
        parser.add_argument("--config-hash", required=True, help="Hash of candidate config")
        parser.add_argument("--operator", required=True, help="Operator identifier")
        parser.add_argument("--config", default="evals/routing/config.yaml", help="Path to config.yaml")
        parser.add_argument("--log", help="Path to heldout_registry.log")
        args = parser.parse_args(rest)
        return handle_preregister(args)

    if subcommand == "report":
        parser = argparse.ArgumentParser(prog="python -m evals.routing report")
        parser.add_argument("--run", required=True, help="Path to run directory")
        parser.add_argument("--baseline", help="Optional baseline run directory")
        parser.add_argument("--dataset", help="Optional path to dataset JSONL")
        parser.add_argument("--config", help="Optional path to config.yaml")
        parser.add_argument("--target", default="policy", help="Target type (policy, replay, deployed)")
        args = parser.parse_args(rest)
        return handle_report(args)

    if subcommand == "compare":
        parser = argparse.ArgumentParser(prog="python -m evals.routing compare")
        parser.add_argument("--baseline", required=True, help="Path to baseline run directory")
        parser.add_argument("--candidate", required=True, help="Path to candidate run directory")
        parser.add_argument("--dataset", help="Optional path to dataset JSONL")
        parser.add_argument("--config", help="Optional path to config.yaml")
        parser.add_argument("--log", help="Path to hillclimb_log.jsonl")
        args = parser.parse_args(rest)
        return handle_compare(args)

    print(f"Unknown subcommand '{subcommand}'. Available: data, run, report, compare, explain, preregister, smoke", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
