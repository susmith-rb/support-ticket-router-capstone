"""End-to-end hermetic evaluation smoke test runner.

# REQ-REVAL-021: Hermetic tests & CI smoke.
Runs the complete evaluation pipeline hermetically on <= 40 test cases:
- Ingests fixture dataset and pre-recorded baseline & candidate traces
- Computes metrics (verified and provisional views)
- Evaluates hard gates and secondary targets
- Executes paired baseline vs candidate comparison with bootstrap CIs
- Performs error failure clustering and medoid selection
- Writes immutable scorecard.json and report.md artifacts to temporary directory
Runs in < 15 seconds with zero network calls.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from evals.routing.report import write_run_artifacts
from evals.routing.schema import read_jsonl

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "evals" / "routing" / "fixtures" / "smoke"


def run_smoke(
    out_dir: Optional[Path] = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Execute end-to-end evaluation harness smoke test.

    Args:
        out_dir: Optional directory to output artifacts. Defaults to a temporary directory.
        quiet: If True, suppress stdout printing.

    Returns:
        Dictionary containing execution summary, scorecard paths, and report text.
    """
    t_start = time.perf_counter()

    cases_path = FIXTURES_DIR / "smoke_cases.jsonl"
    baseline_traces_path = FIXTURES_DIR / "baseline_traces.jsonl"
    candidate_traces_path = FIXTURES_DIR / "candidate_traces.jsonl"

    if not cases_path.exists():
        raise FileNotFoundError(f"Missing smoke cases fixture: {cases_path}")
    if not baseline_traces_path.exists():
        raise FileNotFoundError(f"Missing baseline traces fixture: {baseline_traces_path}")
    if not candidate_traces_path.exists():
        raise FileNotFoundError(f"Missing candidate traces fixture: {candidate_traces_path}")

    cases = read_jsonl(cases_path)

    baseline_traces: list[dict] = []
    with baseline_traces_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                baseline_traces.append(json.loads(line))

    candidate_traces: list[dict] = []
    with candidate_traces_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                candidate_traces.append(json.loads(line))

    # Load configuration
    cfg_file = REPO_ROOT / "evals" / "routing" / "config.yaml"
    cfg: Dict[str, Any] = {}
    if cfg_file.exists():
        import yaml

        with cfg_file.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # Fast bootstrap resamples for smoke execution (<15s)
    cfg["bootstrap_resamples"] = 50
    cfg["created_at"] = "2026-10-28T12:00:00Z"

    temp_ctx = None
    if out_dir is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="routing_smoke_")
        target_dir = Path(temp_ctx.name)
    else:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

    # Use isolated log path inside smoke target_dir to avoid polluting real repo path
    cfg["hillclimb_log_path"] = str(target_dir / "hillclimb_log.jsonl")

    base_dir = target_dir / "run_001_baseline"
    cand_dir = target_dir / "run_002_candidate"

    if not quiet:
        print(f"Running smoke test on {len(cases)} cases...")

    # 1. Baseline run
    base_scorecard = write_run_artifacts(
        base_dir,
        baseline_traces,
        cases,
        cfg=cfg,
        target="policy",
        config_snapshot_extra={"run_id": "run_001_baseline", "dataset_path": str(cases_path)},
    )

    # 2. Candidate run with baseline comparison
    cand_scorecard = write_run_artifacts(
        cand_dir,
        candidate_traces,
        cases,
        cfg=cfg,
        baseline_run_dir=base_dir,
        target="policy",
        config_snapshot_extra={"run_id": "run_002_candidate", "dataset_path": str(cases_path)},
    )

    cand_report_path = cand_dir / "report.md"
    cand_report_text = cand_report_path.read_text(encoding="utf-8")

    elapsed = time.perf_counter() - t_start

    if not quiet:
        print(f"Smoke test completed in {elapsed:.2f}s.")
        print(f"Candidate Overall Gates Passed: {cand_scorecard.get('gates', {}).get('overall_passed')}")
        if cand_scorecard.get("comparison"):
            print(f"Comparison Verdict: {cand_scorecard['comparison'].get('verdict')}")

    result = {
        "elapsed_seconds": elapsed,
        "cases_count": len(cases),
        "target_dir": str(target_dir),
        "baseline_dir": str(base_dir),
        "candidate_dir": str(cand_dir),
        "baseline_scorecard": base_scorecard,
        "candidate_scorecard": cand_scorecard,
        "candidate_report_path": str(cand_report_path),
        "candidate_report_sample": "\n".join(cand_report_text.splitlines()[:60]),
    }

    if temp_ctx is not None:
        # Keep directory if requested or caller cleans it up
        result["_temp_ctx"] = temp_ctx

    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for running evaluation smoke test."""
    del argv
    try:
        res = run_smoke(quiet=False)
        if res["elapsed_seconds"] >= 15.0:
            print(
                f"Warning: Smoke test took {res['elapsed_seconds']:.2f}s, exceeding 15s budget.",
                file=sys.stderr,
            )
        return 0
    except Exception as e:
        print(f"Error during smoke test execution: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
