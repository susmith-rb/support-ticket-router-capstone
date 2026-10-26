#!/usr/bin/env python3
"""ci_eval_gate.py — Automated Retrieval Quality Evaluation Gate for CI/CD.

Enforces retrieval performance gates on the held-out DEV query set prior to
promotion and deployment, following ai-fde-delivery and TDD §6.4:
- Consensus recall@5 >= 0.90 (TDD target)
- Consensus MRR@10 >= 0.75 (TDD target)
- Graded nDCG@5 non-regression: nDCG@5 >= baseline - 0.02

Exit codes (per ai-fde-delivery script contract):
- 0: Evaluation passed all gate thresholds.
- 1: Evaluation ran successfully but failed one or more gate thresholds.
- 2: Evaluation could not run (missing input files, infrastructure failure).
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).parent))
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.models import model_id  # noqa: E402

try:
    import multi_label_eval
    from search_client import SearchClient, clean_query
except ImportError:
    multi_label_eval = None
    SearchClient = None
    clean_query = None


def compute_chain_version(
    code_sha: str = "HEAD",
    prompt_version: str = "v1",
    model_version: Optional[str] = None,
    retriever_index_version: str = "release-3",
    tool_registry_version: str = "v1",
    judge_model_version: Optional[str] = None,
) -> str:
    """Compute 12-char chain_version hash per ai-fde-delivery specification.

    Formula:
        sha256("code_sha|prompt_version|model_version|retriever_index_version|
                tool_registry_version|judge_model_version")[:12]
    """
    m_ver = model_version if model_version is not None else model_id("answer")
    j_ver = judge_model_version if judge_model_version is not None else model_id("judge")
    raw = f"{code_sha}|{prompt_version}|{m_ver}|{retriever_index_version}|{tool_registry_version}|{j_ver}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def load_dev_queries(queries_path: str) -> List[Dict[str, Any]]:
    """Load query objects from JSON or JSONL file."""
    p = pathlib.Path(queries_path)
    if not p.exists():
        raise FileNotFoundError(f"Queries file not found: {queries_path}")

    queries: List[Dict[str, Any]] = []
    text = p.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        queries = json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                queries.append(json.loads(line))
    return queries


def load_baseline(baseline_path: Optional[str]) -> Dict[str, float]:
    """Load baseline metrics or return defaults."""
    default_baseline = {
        "recall@5": 0.900,
        "mrr@10": 0.750,
        "ndcg@5": 0.650,  # Current measured empirical baseline
        "p95_latency_ms": 400.0,
    }
    if not baseline_path:
        return default_baseline

    p = pathlib.Path(baseline_path)
    if not p.exists():
        raise FileNotFoundError(f"Baseline file specified but not found: {baseline_path}")

    data = json.loads(p.read_text(encoding="utf-8"))
    if "metrics" in data and isinstance(data["metrics"], dict):
        data = data["metrics"]
    return {**default_baseline, **data}


def _query_id(q: Dict[str, Any]) -> str:
    """Extract query identifier with priority: qid -> query_id -> id -> str(thread_id)."""
    raw_id = (
        q.get("qid")
        or q.get("query_id")
        or q.get("id")
        or (str(q["thread_id"]) if "thread_id" in q and q["thread_id"] is not None else "")
    )
    return str(raw_id).strip()


def _query_text(q: Dict[str, Any]) -> str:
    """Extract query text with priority: query -> text."""
    return str(q.get("query") or q.get("text") or "").strip()


def evaluate_system_results(
    system_top10: Dict[str, List[str]],
    labels_c_path: Optional[str],
    queries: List[Dict[str, Any]],
    scope: str = "MAIN",
    split: str = "dev",
) -> Dict[str, float]:
    """Calculate retrieval metrics via multi_label_eval or fallback matching."""
    if labels_c_path and pathlib.Path(labels_c_path).exists() and multi_label_eval:
        labels_c = multi_label_eval.load_label_file(labels_c_path)
        eval_res = multi_label_eval.evaluate_multilabel(
            system_top10=system_top10,
            labels_c=labels_c,
            scope=scope,
            split=split,
        )
        scope_res = eval_res.get(scope) if isinstance(eval_res.get(scope), dict) else eval_res
        cons = scope_res.get("consensus", {})
        return {
            "recall@5": cons.get("recall@5", 0.0),
            "mrr@10": cons.get("mrr@10", 0.0),
            "ndcg@5": cons.get("ndcg@5", 0.0),
            "n_evaluated": scope_res.get("n_evaluated", len(system_top10)),
        }

    # Fallback single-gold matching if multi-annotator labels are unavailable
    recalls = []
    mrrs = []
    for q in queries:
        qid = _query_id(q)
        gold = (
            q.get("gold_doc")
            or q.get("primary")
            or q.get("relevant_doc")
            or q.get("relevant")
        )
        retrieved = system_top10.get(qid, [])
        if not gold:
            continue

        if isinstance(gold, (list, tuple, set)):
            gold_set = set(gold)
            hit = 1.0 if any(d in gold_set for d in retrieved[:5]) else 0.0
            recalls.append(hit)
            mrr = 0.0
            for rank, doc in enumerate(retrieved[:10], 1):
                if doc in gold_set:
                    mrr = 1.0 / rank
                    break
            mrrs.append(mrr)
        else:
            hit = 1.0 if gold in retrieved[:5] else 0.0
            recalls.append(hit)
            mrr = 0.0
            for rank, doc in enumerate(retrieved[:10], 1):
                if doc == gold:
                    mrr = 1.0 / rank
                    break
            mrrs.append(mrr)

    mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
    mean_mrr = sum(mrrs) / len(mrrs) if mrrs else 0.0
    return {
        "recall@5": mean_recall,
        "mrr@10": mean_mrr,
        "ndcg@5": mean_mrr * 0.9,  # Approximation when multi-level gains are absent
        "n_evaluated": len(recalls),
    }


def execute_gate_check(
    metrics: Dict[str, float],
    baseline: Dict[str, float],
    min_recall: float = 0.90,
    min_mrr: float = 0.75,
    max_ndcg_drop: float = 0.02,
) -> Tuple[bool, List[str]]:
    """Evaluate metric values against absolute and non-regression thresholds."""
    details = []
    passed = True

    # 1. Consensus recall@5 >= min_recall (0.90)
    rec = metrics.get("recall@5", 0.0)
    if rec >= min_recall:
        details.append(f"PASS: recall@5 = {rec:.4f} >= {min_recall:.4f}")
    else:
        passed = False
        details.append(f"FAIL: recall@5 = {rec:.4f} < {min_recall:.4f} (target)")

    # 2. Consensus MRR@10 >= min_mrr (0.75)
    mrr = metrics.get("mrr@10", 0.0)
    if mrr >= min_mrr:
        details.append(f"PASS: mrr@10 = {mrr:.4f} >= {min_mrr:.4f}")
    else:
        passed = False
        details.append(f"FAIL: mrr@10 = {mrr:.4f} < {min_mrr:.4f} (target)")

    # 3. Graded nDCG@5 non-regression (>= baseline - max_ndcg_drop)
    ndcg = metrics.get("ndcg@5", 0.0)
    base_ndcg = baseline.get("ndcg@5", 0.650)
    min_allowed_ndcg = base_ndcg - max_ndcg_drop
    if ndcg >= min_allowed_ndcg:
        details.append(
            f"PASS: ndcg@5 = {ndcg:.4f} >= {min_allowed_ndcg:.4f} "
            f"(baseline {base_ndcg:.4f} - max drop {max_ndcg_drop:.4f})"
        )
    else:
        passed = False
        details.append(
            f"FAIL: ndcg@5 = {ndcg:.4f} < {min_allowed_ndcg:.4f} "
            f"(regressed below baseline {base_ndcg:.4f} - {max_ndcg_drop:.4f})"
        )

    return passed, details


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CI/CD Golden Retrieval Evaluation Gate")
    parser.add_argument("--queries", required=False, default=None, help="Path to dev queries file")
    parser.add_argument("--labels-c", required=False, default=None, help="Path to Adjudicator C labels")
    parser.add_argument("--gold-map", required=False, default=None, help="Path to gold map JSON")
    parser.add_argument("--baseline", required=False, default=None, help="Path to baseline metrics JSON")
    parser.add_argument("--system-run", required=False, default=None, help="Path to existing system run JSON")
    parser.add_argument(
        "--config",
        default=None,
        help="Configuration name in system run (default: None, auto-selects production config)",
    )
    parser.add_argument("--project", default="susmith-fde-ticket-router", help="GCP Project ID")
    parser.add_argument("--engine", default="support-kb-search", help="Search engine ID")
    parser.add_argument("--location", default="global", help="Location (global)")
    parser.add_argument("--scope", default="MAIN", choices=["MAIN", "ALL-support"], help="Evaluation scope")
    parser.add_argument("--split", default="dev", choices=["dev", "test"], help="Dataset split")
    parser.add_argument("--min-recall", type=float, default=0.90, help="Minimum recall@5 target")
    parser.add_argument("--min-mrr", type=float, default=0.75, help="Minimum MRR@10 target")
    parser.add_argument("--max-ndcg-drop", type=float, default=0.02, help="Max allowed drop in nDCG@5 vs baseline")
    parser.add_argument("--out", default="ci_eval_gate_result.json", help="Path to write results JSON")
    parser.add_argument("--model-version", default=model_id("answer"), help="Model version for chain version hash")
    parser.add_argument("--judge-model-version", default=model_id("judge"), help="Judge model version for chain version hash")
    parser.add_argument("--enforce-gate", action="store_true", default=True, help="Exit 1 on gate failure")
    parser.add_argument("--no-enforce-gate", action="store_false", dest="enforce_gate")
    parser.add_argument("--dry-run", action="store_true", help="Print expected actions without running")
    args = parser.parse_args(argv)

    print("=" * 70)
    print("CI/CD GOLDEN RETRIEVAL EVALUATION GATE")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    try:
        # Load baseline
        baseline = load_baseline(args.baseline)

        # Obtain system retrieval top-10 candidates
        system_top10: Dict[str, List[str]] = {}
        queries: List[Dict[str, Any]] = []

        if args.queries:
            p = pathlib.Path(args.queries)
            if not p.exists():
                sys.stderr.write(f"Error: Missing input queries file: {args.queries}\n")
                return 2
            queries = load_dev_queries(args.queries)
            print(f"Loaded {len(queries)} queries from: {args.queries}")

        if args.system_run:
            p = pathlib.Path(args.system_run)
            if not p.exists():
                sys.stderr.write(f"Error: Missing input system run file: {args.system_run}\n")
                return 2
            print(f"Loading system results from: {args.system_run}")
            if multi_label_eval:
                try:
                    system_top10 = multi_label_eval.load_system_rows(
                        args.system_run, config_name=args.config
                    )
                except Exception as e:
                    sys.stderr.write(f"Warning: load_system_rows error: {e}, falling back to raw load\n")
                    system_top10 = {}
            if not system_top10:
                with open(args.system_run, "r", encoding="utf-8") as f:
                    run_data = json.load(f)
                if isinstance(run_data, dict) and "system_top10" in run_data:
                    system_top10 = run_data["system_top10"]
                elif isinstance(run_data, dict) and args.config and args.config in run_data:
                    system_top10 = run_data[args.config].get("system_top10", {})
                elif isinstance(run_data, dict):
                    system_top10 = run_data

        if args.labels_c:
            p = pathlib.Path(args.labels_c)
            if not p.exists():
                sys.stderr.write(f"Error: Missing input labels-c file: {args.labels_c}\n")
                return 2

        if not system_top10 and queries:
            if not SearchClient:
                sys.stderr.write("Error: search_client not available to query live Discovery Engine\n")
                return 2
            client = SearchClient(
                project=args.project,
                engine=args.engine,
                location=args.location,
            )
            print(f"Executing search requests against {args.project}/{args.engine} ({args.location})...")
            empty_query_count = 0
            for q in queries:
                qid = _query_id(q)
                raw_text = _query_text(q)
                if not raw_text:
                    empty_query_count += 1
                    sys.stderr.write(f"Warning: Empty query text for query ID '{qid}'\n")
                    continue
                qtext = clean_query(raw_text) if clean_query else raw_text
                if not qtext:
                    empty_query_count += 1
                    sys.stderr.write(f"Warning: Cleaned query text is empty for query ID '{qid}'\n")
                    continue
                res = client.search(qtext, threshold="LOWEST", page_size=10)
                system_top10[qid] = list(res.document_ids[:10])

            if empty_query_count > 0:
                sys.stderr.write(
                    f"Error: {empty_query_count} query/queries had empty text. "
                    "Refusing to execute evaluation gate with missing query text.\n"
                )
                return 2  # Exit code 2: Gate could not run
        elif not system_top10 and not queries:
            sys.stderr.write(
                "Error: Neither --system-run nor valid --queries file provided.\n"
            )
            return 2  # Exit code 2: Gate could not run

        # Compute metrics
        metrics = evaluate_system_results(
            system_top10=system_top10,
            labels_c_path=args.labels_c,
            queries=queries,
            scope=args.scope,
            split=args.split,
        )

        # Enforce gate criteria
        passed, details = execute_gate_check(
            metrics=metrics,
            baseline=baseline,
            min_recall=args.min_recall,
            min_mrr=args.min_mrr,
            max_ndcg_drop=args.max_ndcg_drop,
        )

        verdict = "PASS" if passed else "FAIL"
        chain_version = compute_chain_version(
            model_version=args.model_version,
            judge_model_version=args.judge_model_version,
        )

        print(f"\nChain Version: {chain_version}")
        print(f"Evaluated Queries: {metrics.get('n_evaluated', 0)}")
        print("\nGate Threshold Verification:")
        for line in details:
            print(f"  {line}")
        print(f"\nFinal Verdict: [{verdict}]")
        print("=" * 70)

        # Save results
        out_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chain_version": chain_version,
            "verdict": verdict,
            "passed": passed,
            "scope": args.scope,
            "split": args.split,
            "system_top10": system_top10,
            "metrics": metrics,
            "baseline": baseline,
            "gate_thresholds": {
                "min_recall@5": args.min_recall,
                "min_mrr@10": args.min_mrr,
                "max_ndcg_drop": args.max_ndcg_drop,
            },
            "gate_details": details,
        }
        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")
        print(f"Gate evaluation results saved to: {out_path}")

        if not passed and args.enforce_gate:
            print("\nCRITICAL: Golden Eval Gate failed. Blocking pipeline execution.")
            return 1  # Exit code 1: Gate failed

        return 0  # Exit code 0: Passed

    except FileNotFoundError as e:
        sys.stderr.write(f"Configuration or input file missing: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"Unexpected error running golden eval gate: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
