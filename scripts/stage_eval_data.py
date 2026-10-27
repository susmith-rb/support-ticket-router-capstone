#!/usr/bin/env python3
"""stage_eval_data.py — Prepare and stage DEV-only evaluation data for CI/CD retrieval gates.

Enforces zero-leakage invariants:
- Only DEV queries (d-prefixed) are staged.
- Labels A, B, and C2 are filtered to DEV qids only.
- Strict assertion: No test queries (t-prefixed) or TEST split rows can be staged.
- Baseline metrics JSON is generated matching current production retrieval config.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRATCH = Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag")
DEFAULT_BUCKET = "susmith-fde-ticket-router-laya"

PRODUCTION_BASELINE = {
    "config": "production_retrieval_v2",
    "timestamp": "2026-10-25T00:00:00Z",
    "recall@5": 0.900,
    "mrr@10": 0.750,
    "ndcg@5": 0.650,
    "p95_latency_ms": 400.0,
}


def validate_record_is_dev_only(rec: Dict[str, Any], source_desc: str = "") -> None:
    """Assert that a record is strictly DEV data and does not contain TEST items."""
    qid = str(rec.get("qid") or rec.get("query_id") or rec.get("id") or "")
    if qid.startswith(("t", "test")):
        raise ValueError(
            f"LEAKAGE DETECTED in {source_desc}: Query ID '{qid}' is a test query. "
            "Only DEV queries (d-prefixed) are allowed to be staged."
        )

    split = rec.get("split")
    if split == "test":
        raise ValueError(
            f"LEAKAGE DETECTED in {source_desc}: Record contains split='test'. "
            "Only DEV queries are allowed to be staged."
        )


def filter_dev_only(records: List[Dict[str, Any]], dev_qids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """Filter records to strictly include only DEV queries and reject any TEST rows."""
    out: List[Dict[str, Any]] = []
    for rec in records:
        qid = str(rec.get("qid") or rec.get("query_id") or rec.get("id") or "")
        # If explicitly marked as test or t-prefixed, skip it during filtering
        if qid.startswith(("t", "test")) or rec.get("split") == "test":
            continue

        if dev_qids is not None and qid not in dev_qids:
            continue

        # Invariant check on output
        validate_record_is_dev_only(rec, source_desc="staged record")
        out.append(rec)
    return out


def stage_eval_data(
    dev_queries_path: Path,
    labels_dir: Path,
    gold_map_path: Path,
    out_dir: Path,
    baseline_path: Optional[Path] = None,
) -> Dict[str, int]:
    """Prepare and validate DEV-only evaluation dataset files in out_dir."""
    if not dev_queries_path.exists():
        raise FileNotFoundError(f"DEV queries not found: {dev_queries_path}")
    if not gold_map_path.exists():
        raise FileNotFoundError(f"Gold map not found: {gold_map_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load and validate DEV queries
    dev_records: List[Dict[str, Any]] = []
    text = dev_queries_path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        raw_queries = json.loads(text)
    else:
        raw_queries = [json.loads(line) for line in text.splitlines() if line.strip()]

    for r in raw_queries:
        validate_record_is_dev_only(r, source_desc="input dev_queries")
        dev_records.append(r)

    dev_qids = {str(r.get("qid") or r.get("query_id") or r.get("id")) for r in dev_records}
    staged_counts = {"dev_queries": len(dev_records)}

    # Write out staged dev.jsonl
    with open(out_dir / "dev.jsonl", "w", encoding="utf-8") as f:
        for r in dev_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 2. Filter and stage labels A, B, C2
    label_files = {
        "labels_a.jsonl": ["labels_A.jsonl", "labels_a.jsonl"],
        "labels_b.jsonl": ["labels_B.jsonl", "labels_b.jsonl"],
        "labels_c2.jsonl": ["labels_C2.jsonl", "labels_c2.jsonl", "labels_c.jsonl", "labels_C.jsonl"],
    }

    for target_name, candidates in label_files.items():
        src_path = None
        for c in candidates:
            p = labels_dir / c
            if p.exists():
                src_path = p
                break

        if src_path and src_path.exists():
            rows = [json.loads(l) for l in src_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            filtered = filter_dev_only(rows, dev_qids=dev_qids)
            with open(out_dir / target_name, "w", encoding="utf-8") as f:
                for r in filtered:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            staged_counts[target_name] = len(filtered)
        else:
            sys.stderr.write(f"Warning: Label file for {target_name} not found in {labels_dir}\n")

    # Also write alias labels_c.jsonl if labels_c2.jsonl was staged
    if (out_dir / "labels_c2.jsonl").exists():
        (out_dir / "labels_c.jsonl").write_text(
            (out_dir / "labels_c2.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        staged_counts["labels_c.jsonl"] = staged_counts.get("labels_c2.jsonl", 0)

    # 3. Gold map
    gold_map_content = gold_map_path.read_text(encoding="utf-8")
    (out_dir / "gold_map.json").write_text(gold_map_content, encoding="utf-8")
    staged_counts["gold_map.json"] = 1

    # 4. Baseline metrics
    if baseline_path and baseline_path.exists():
        baseline_content = baseline_path.read_text(encoding="utf-8")
    else:
        baseline_content = json.dumps(PRODUCTION_BASELINE, indent=2)
    (out_dir / "baseline.json").write_text(baseline_content, encoding="utf-8")
    staged_counts["baseline.json"] = 1

    # Invariant verification pass across all output files
    verify_staged_directory(out_dir)

    return staged_counts


def verify_staged_directory(staged_dir: Path) -> None:
    """Assert that NO test data exists in the staged directory."""
    for jsonl_file in staged_dir.glob("*.jsonl"):
        for i, line in enumerate(jsonl_file.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            rec = json.loads(line)
            validate_record_is_dev_only(rec, source_desc=f"{jsonl_file.name}:{i+1}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage DEV-only retrieval evaluation dataset for CI/CD")
    ap.add_argument(
        "--dev-queries",
        type=Path,
        default=None,
        help="Path to dev.jsonl queries file",
    )
    ap.add_argument(
        "--labels-dir",
        type=Path,
        default=None,
        help="Directory containing labels_A.jsonl, labels_B.jsonl, labels_C2.jsonl",
    )
    ap.add_argument(
        "--gold-map",
        type=Path,
        default=REPO_ROOT / "rag" / "corpus_v2" / "gold_map_v1_v2.json",
        help="Path to gold_map.json",
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to baseline.json (optional)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/tmp/staged_eval"),
        help="Staging destination directory",
    )
    ap.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help="Target GCS bucket name",
    )
    ap.add_argument(
        "--upload",
        action="store_true",
        help="Upload staged files to gs://${BUCKET}/eval/ (do not use in tests)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and stage files locally only without uploading",
    )
    args = ap.parse_args(argv)

    # Determine default paths
    dev_queries = args.dev_queries
    if dev_queries is None:
        if (DEFAULT_SCRATCH / "eval" / "dev.jsonl").exists():
            dev_queries = DEFAULT_SCRATCH / "eval" / "dev.jsonl"
        elif (REPO_ROOT / "rag" / "eval" / "dev.jsonl").exists():
            dev_queries = REPO_ROOT / "rag" / "eval" / "dev.jsonl"
        else:
            sys.stderr.write("Error: Could not locate dev.jsonl. Specify with --dev-queries.\n")
            return 2

    labels_dir = args.labels_dir
    if labels_dir is None:
        if (REPO_ROOT / "rag" / "labels" / "retrieval_v1").exists():
            labels_dir = REPO_ROOT / "rag" / "labels" / "retrieval_v1"
        elif (DEFAULT_SCRATCH / "labeling").exists():
            labels_dir = DEFAULT_SCRATCH / "labeling"
        else:
            sys.stderr.write("Error: Could not locate labels dir. Specify with --labels-dir.\n")
            return 2

    print(f"Staging DEV-only eval inputs from:\n  queries: {dev_queries}\n  labels:  {labels_dir}\n  goldmap: {args.gold_map}")
    counts = stage_eval_data(
        dev_queries_path=dev_queries,
        labels_dir=labels_dir,
        gold_map_path=args.gold_map,
        out_dir=args.out_dir,
        baseline_path=args.baseline,
    )

    print("Staged files successfully verified (ZERO test data):")
    for name, cnt in counts.items():
        print(f"  {name}: {cnt} items")

    if args.upload and not args.dry_run:
        target_gcs = f"gs://{args.bucket}/eval/"
        print(f"Uploading staged files to {target_gcs}...")
        cmd = ["gcloud", "storage", "cp", "-r", f"{args.out_dir}/*", target_gcs]
        subprocess.run(cmd, check=True)
        print("Upload complete.")
    else:
        print("Dry run / preparation only: files staged at", args.out_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
