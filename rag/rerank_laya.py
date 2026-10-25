#!/usr/bin/env python3
"""rag/rerank_laya.py — Laya typed-decision choice reranker (Arm B, PLAN7).

Provides:
1. Dataset conversion: transforms multi-candidate retrieval training/val data
   (with graded relevance 2=primary, 1=secondary, 0=negative) into laya-train JSONL
   items with <= 5 options (positives + hardest negatives) and normalized soft gold.
2. Inference reranking: given an inbound customer message and top-5 candidates
   (from initial Agent Search retrieval), scores candidates with Laya choice head
   and reorders top-5 by probability, keeping ranks 6-10 in original order.
"""

import argparse
import json
import math
import pathlib
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union


QUESTION_ID = "article"
QUESTION_INSTRUCTIONS = "Which FAQ article best answers or addresses the customer inquiry?"
MAX_OPTIONS = 5


def format_scope_phrase(doc: Dict[str, Any], max_len: int = 80) -> str:
    """Extract a short scope phrase from a docs.json entry."""
    also = doc.get("also_asked", [])
    if also and isinstance(also, list) and len(also) > 0 and str(also[0]).strip():
        phrase = str(also[0]).strip()
    else:
        prefix = doc.get("answer_prefix", "").strip()
        first_line = prefix.split("\n")[0].strip()
        phrase = first_line.split(". ")[0].strip()
    if len(phrase) > max_len:
        phrase = phrase[: max_len - 3].rstrip() + "..."
    return phrase


def format_option_text(doc_id: str, doc: Dict[str, Any]) -> str:
    """Format an article option: title + short scope phrase from docs.json."""
    title = str(doc.get("title", "")).strip() or doc_id
    scope = format_scope_phrase(doc)
    if scope:
        return f"{title} — {scope}"
    return title


def select_options(
    candidates: Sequence[str],
    rels: Dict[str, int],
    max_options: int = MAX_OPTIONS,
) -> List[str]:
    """Select up to max_options from candidates: all positives plus hardest negatives.

    Candidates list is ordered by search rank / hardness.
    Positives are doc_ids with rels.get(doc_id, 0) > 0.
    Negatives are doc_ids with rels.get(doc_id, 0) <= 0.
    """
    positives = [c for c in candidates if rels.get(c, 0) > 0]
    negatives = [c for c in candidates if rels.get(c, 0) <= 0]

    # If positives exceed max_options, take highest grades first
    if len(positives) > max_options:
        positives.sort(key=lambda c: rels.get(c, 0), reverse=True)
        return positives[:max_options]

    needed_negatives = max_options - len(positives)
    selected = list(positives) + negatives[:needed_negatives]
    return selected[:max_options]


def normalize_soft_gold(
    selected_doc_ids: Sequence[str],
    rels: Dict[str, int],
    epsilon: float = 0.0,
) -> Dict[str, float]:
    """Compute normalized soft gold distribution over selected options.

    Grade mapping:
      primary: grade >= 2 -> weight 2.0
      relevant / secondary: grade == 1 -> weight 1.0
      others / negatives: grade <= 0 -> weight 0.0 (or epsilon if specified)

    If sum of weights == 0, returns uniform distribution across options.
    """
    if not selected_doc_ids:
        return {}

    weights: Dict[str, float] = {}
    for doc_id in selected_doc_ids:
        grade = rels.get(doc_id, 0)
        if grade >= 2:
            w = 2.0
        elif grade == 1:
            w = 1.0
        else:
            w = float(epsilon)
        weights[doc_id] = w

    total = sum(weights.values())
    if total <= 0.0:
        uniform_p = 1.0 / len(selected_doc_ids)
        return {doc_id: round(uniform_p, 6) for doc_id in selected_doc_ids}

    probs = {doc_id: round(w / total, 6) for doc_id, w in weights.items()}
    # Adjust any rounding discrepancy on the maximum weight entry
    max_k = max(probs, key=lambda k: (weights[k], probs[k]))
    probs[max_k] = round(probs[max_k] + (1.0 - sum(probs.values())), 6)
    return probs


def build_laya_choice_question(
    option_doc_ids: Sequence[str],
    docs: Dict[str, Dict[str, Any]],
    instructions: str = QUESTION_INSTRUCTIONS,
) -> Dict[str, Any]:
    """Build Laya choice question structure for option doc_ids."""
    criteria: Dict[str, str] = {}
    for doc_id in option_doc_ids:
        doc = docs.get(doc_id, {"title": doc_id})
        criteria[doc_id] = format_option_text(doc_id, doc)
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": criteria,
    }


def convert_row_to_laya(
    row: Dict[str, Any],
    docs: Dict[str, Dict[str, Any]],
    max_options: int = MAX_OPTIONS,
    max_state_chars: int = 1800,
    instructions: str = QUESTION_INSTRUCTIONS,
) -> Dict[str, Any]:
    """Convert a single rerank candidate row into a laya-train JSONL item."""
    query = row.get("query", "").strip()
    rels = row.get("rels", {})
    candidates = row.get("candidates", [])

    options = select_options(candidates, rels, max_options=max_options)
    gold_probs = normalize_soft_gold(options, rels)

    best_opt = max(options, key=lambda o: (gold_probs.get(o, 0.0), rels.get(o, 0))) if options else ""

    question_def = build_laya_choice_question(options, docs, instructions=instructions)
    return {
        "id": str(row.get("qid") or row.get("thread_id") or "item"),
        "state": query[:max_state_chars],
        "questions": {
            QUESTION_ID: question_def,
        },
        "gold": {
            QUESTION_ID: {
                "probabilities": gold_probs,
            }
        },
        "expected": {
            QUESTION_ID: best_opt,
        },
    }


def load_docs(docs_path: Union[str, pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    """Load docs catalog JSON."""
    with open(docs_path, "r", encoding="utf-8") as f:
        return json.load(f)


def convert_dataset_file(
    input_jsonl_path: Union[str, pathlib.Path],
    docs_json_path: Union[str, pathlib.Path],
    output_jsonl_path: Union[str, pathlib.Path],
    max_options: int = MAX_OPTIONS,
) -> int:
    """Convert input train/val JSONL into laya-train JSONL."""
    docs = load_docs(docs_json_path)
    count = 0
    out_p = pathlib.Path(output_jsonl_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(input_jsonl_path, "r", encoding="utf-8") as fin, \
         open(out_p, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            row = json.loads(line)
            laya_item = convert_row_to_laya(row, docs, max_options=max_options)
            fout.write(json.dumps(laya_item) + "\n")
            count += 1
    return count


def rerank_top5_candidates(
    query: str,
    top_candidates: Sequence[str],
    agent_or_predict_fn: Any,
    docs: Dict[str, Dict[str, Any]],
    k_rerank: int = 5,
) -> Tuple[List[str], Dict[str, float]]:
    """Reorder top-5 candidates by Laya choice probability, keeping ranks 6-10 after them.

    Args:
        query: Inbound customer message text.
        top_candidates: List of candidate doc IDs (e.g. top 10 from initial retrieval).
        agent_or_predict_fn: A Laya agent instance with a .predict() method, or a callable
            taking (query, questions) -> dict.
        docs: Article catalog mapping doc_id -> doc metadata.
        k_rerank: Number of top candidates to reorder (<= 5).

    Returns:
        (reordered_candidates, probabilities_dict)
    """
    candidates = list(top_candidates)
    if len(candidates) <= 1:
        probs = {candidates[0]: 1.0} if candidates else {}
        return candidates, probs

    top_k = candidates[:k_rerank]
    tail = candidates[k_rerank:]

    question_def = build_laya_choice_question(top_k, docs)
    questions = {QUESTION_ID: question_def}

    try:
        resp = agent_or_predict_fn.predict(query, questions)
    except AttributeError:
        if callable(agent_or_predict_fn):
            resp = agent_or_predict_fn(query, questions)
        else:
            raise TypeError(
                f"agent_or_predict_fn must have .predict or be callable, got {type(agent_or_predict_fn)}"
            )

    answers = resp.get("answers", resp)
    art_ans = answers.get(QUESTION_ID, {})
    probs = art_ans.get("probabilities", {})

    # Reorder top_k by probability descending (preserve original index on ties)
    indexed_candidates = list(enumerate(top_k))
    indexed_candidates.sort(
        key=lambda item: (float(probs.get(item[1], 0.0)), -item[0]),
        reverse=True,
    )
    reordered_top_k = [item[1] for item in indexed_candidates]

    return reordered_top_k + tail, {k: float(v) for k, v in probs.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Laya choice reranker utilities.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # Convert subcommand
    conv_p = subparsers.add_parser("convert", help="Convert train/val datasets to laya-train format.")
    conv_p.add_argument("--train-in", required=True, help="Input train.jsonl path.")
    conv_p.add_argument("--val-in", required=True, help="Input val.jsonl path.")
    conv_p.add_argument("--docs", required=True, help="Input docs.json path.")
    conv_p.add_argument("--out-dir", required=True, help="Output directory for laya JSONL splits.")

    # Rerank DEV subcommand
    dev_p = subparsers.add_parser("rerank-dev", help="Rerank DEV candidate rows using a Laya checkpoint.")
    dev_p.add_argument("--candidates", required=True, help="Path to dev_candidates.jsonl.")
    dev_p.add_argument("--docs", required=True, help="Path to docs.json.")
    dev_p.add_argument("--ckpt", required=True, help="Path or Hub ID to fine-tuned Laya checkpoint.")
    dev_p.add_argument("--subfolder", default=None, help="Optional checkpoint subfolder.")
    dev_p.add_argument("--out", required=True, help="Output JSON path for dev_rows_laya.json.")
    dev_p.add_argument("--config-name", default="laya_choice_top5", help="Config name in results JSON.")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.subcommand == "convert":
        out_dir = pathlib.Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        n_train = convert_dataset_file(
            args.train_in, args.docs, out_dir / "train.jsonl"
        )
        n_val = convert_dataset_file(
            args.val_in, args.docs, out_dir / "val.jsonl"
        )
        print(f"Converted {n_train} train rows -> {out_dir / 'train.jsonl'}")
        print(f"Converted {n_val} val rows -> {out_dir / 'val.jsonl'}")

    elif args.subcommand == "rerank-dev":
        import laya
        print(f"Loading checkpoint: {args.ckpt} (subfolder={args.subfolder})...")
        agent = laya.load(args.ckpt, subfolder=args.subfolder) if args.subfolder else laya.load(args.ckpt)
        docs = load_docs(args.docs)

        rows_in = []
        with open(args.candidates, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows_in.append(json.loads(line))

        print(f"Loaded {len(rows_in)} DEV candidate queries. Reranking top-5...")
        # Warmup
        warmup_q = build_laya_choice_question(list(docs.keys())[:5], docs)
        agent.predict("warm up", {QUESTION_ID: warmup_q})

        dev_rows = []
        latencies: List[float] = []
        t0_all = time.perf_counter()

        for idx, row in enumerate(rows_in):
            qid = row.get("qid", f"q{idx}")
            query = row.get("query", "")
            top10 = row.get("top10", [])

            t0 = time.perf_counter()
            reordered_top10, probs = rerank_top5_candidates(
                query=query,
                top_candidates=top10,
                agent_or_predict_fn=agent,
                docs=docs,
                k_rerank=5,
            )
            lat_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat_ms)

            dev_rows.append({
                "qid": qid,
                "query": query,
                "top10": reordered_top10,
                "top10_ids": reordered_top10,
                "initial_top10": top10,
                "latency_ms": round(lat_ms, 2),
                "laya_probs": {k: round(v, 4) for k, v in probs.items()},
            })

        total_time_s = time.perf_counter() - t0_all
        latencies_sorted = sorted(latencies)
        p50 = latencies_sorted[int(len(latencies_sorted) * 0.50)]
        p90 = latencies_sorted[int(len(latencies_sorted) * 0.90)]
        p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)]
        mean_lat = sum(latencies) / len(latencies) if latencies else 0.0

        latency_summary = {
            "count": len(latencies),
            "mean": round(mean_lat, 2),
            "min": round(min(latencies), 2) if latencies else 0.0,
            "max": round(max(latencies), 2) if latencies else 0.0,
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p95": round(p95, 2),
            "total_time_s": round(total_time_s, 2),
        }

        output_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "candidates_path": str(args.candidates),
            "n_queries": len(dev_rows),
            "results": [
                {
                    "config": args.config_name,
                    "description": "Laya choice reranker over top 5 candidates with ranks 6-10 preserved",
                    "status": "completed",
                    "latency_summary": latency_summary,
                    "rows": dev_rows,
                }
            ],
        }

        out_path = pathlib.Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)

        print(f"Reranked {len(dev_rows)} rows written to {out_path}")
        print(f"Latency stats: mean={mean_lat:.2f}ms, p50={p50:.2f}ms, p95={p95:.2f}ms")


if __name__ == "__main__":
    main()

