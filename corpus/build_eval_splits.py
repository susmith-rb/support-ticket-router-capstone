#!/usr/bin/env python3
"""build_eval_splits.py — Construct frozen TEST and disjoint DEV evaluation splits.

Schema for each line in S/eval/{dev,test}.jsonl:
  {"qid": "...", "query": "...", "relevant": "...", "intent": "...", "split": "...", "thread_id": "..."}

Invariants:
- TEST consists of exactly the 300 rows from S/ret_v2b_lowest_clean.json in identical order (qid t000-t299).
- DEV consists of 300 new queries sampled from candidate threads disjoint from TEST and theme examples.
- Neither TEST nor DEV overlaps with any theme's first 15 agent example threads.
- TEST and DEV query and thread sets are strictly disjoint.
"""

import argparse
from collections import Counter
import json
import os
import pathlib
import random
import re
from typing import Any, Dict, List, Optional, Set, Tuple


DEFAULT_SCRATCH = pathlib.Path(
    os.environ.get(
        "SCRATCH_RAG",
        "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag",
    )
)


def extract_theme_candidates(
    themes: List[Dict[str, Any]],
    threads: Dict[str, Dict[str, Any]],
    live_doc_ids: Set[str],
    gold_map: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """Extract candidate queries and record example thread IDs.

    Replicates candidate extraction from retrieval_eval.py.
    Returns:
      (candidates, example_thread_ids)
    """
    mapping = gold_map.get("map", {}) if gold_map else {}
    if not mapping and gold_map:
        mapping = {k: v for k, v in gold_map.items() if k != "dropped" and isinstance(v, str)}

    cands: List[Dict[str, Any]] = []
    example_thread_ids: Set[str] = set()

    for th in themes:
        doc = re.sub(
            r"[^a-z0-9-]", "-", f"faq-{th['intent_family']}-{th['theme_id']}".lower().replace("_", "-")
        )[:120]
        if doc not in live_doc_ids:
            continue
        with_agent = [
            t
            for t in th["thread_ids"]
            if str(t) in threads and any(x["role"] == "agent" for x in threads[str(t)]["turns"])
        ]
        examples = set(str(t) for t in with_agent[:15])
        example_thread_ids.update(examples)

        target = mapping.get(doc, doc) if mapping else doc
        for t in th["thread_ids"]:
            st = str(t)
            if st in threads and st not in examples:
                cands.append(
                    {
                        "thread_id": st,
                        "query": threads[st]["turns"][0]["text"],
                        "original_gold": doc,
                        "relevant": target,
                        "intent": th["intent_family"],
                    }
                )

    return cands, example_thread_ids


def build_test_split(
    test_rows: List[Dict[str, Any]],
    cands: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build TEST split matching exactly the 300 rows in ret_v2b_lowest_clean.json.

    Uses candidate generator seed 13 to identify matching thread_id for each row.
    """
    cands_shuffled = list(cands)
    random.Random(13).shuffle(cands_shuffled)
    test_cands = cands_shuffled[: len(test_rows)]

    test_split: List[Dict[str, Any]] = []
    for i, (row, cand) in enumerate(zip(test_rows, test_cands)):
        # Verify alignment with original ret_v2b_lowest_clean rows
        assert (
            row["query"] == cand["query"]
        ), f"Query mismatch at index {i}: {row['query'][:40]} vs {cand['query'][:40]}"
        assert (
            row["relevant"] == cand["relevant"]
        ), f"Target mismatch at index {i}: {row['relevant']} vs {cand['relevant']}"
        assert (
            row["intent"] == cand["intent"]
        ), f"Intent mismatch at index {i}: {row['intent']} vs {cand['intent']}"

        test_split.append(
            {
                "qid": f"t{i:03d}",
                "query": row["query"],
                "relevant": row["relevant"],
                "original_gold": cand["original_gold"],
                "intent": row["intent"],
                "split": "test",
                "thread_id": cand["thread_id"],
            }
        )
    return test_split


def build_dev_split(
    available_cands: List[Dict[str, Any]],
    n_dev: int = 300,
    seed: int = 133,
    stratified_counts: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Build DEV split disjoint from TEST and theme examples."""
    if len(available_cands) < n_dev:
        raise ValueError(
            f"Insufficient available candidates for DEV ({len(available_cands)} < {n_dev})"
        )

    if stratified_counts:
        # Stratify to match specified target counts per intent
        by_intent: Dict[str, List[Dict[str, Any]]] = {}
        for c in available_cands:
            by_intent.setdefault(c["intent"], []).append(c)

        rng = random.Random(seed)
        for intent in by_intent:
            rng.shuffle(by_intent[intent])

        dev_selected: List[Dict[str, Any]] = []
        for intent, count in stratified_counts.items():
            pool = by_intent.get(intent, [])
            if len(pool) < count:
                raise ValueError(
                    f"Not enough candidates for intent '{intent}' ({len(pool)} < {count})"
                )
            dev_selected.extend(pool[:count])
        rng.shuffle(dev_selected)
    else:
        # Candidate logic with seed shuffle
        shuffled = list(available_cands)
        random.Random(seed).shuffle(shuffled)
        dev_selected = shuffled[:n_dev]

    dev_split: List[Dict[str, Any]] = []
    for i, cand in enumerate(dev_selected):
        dev_split.append(
            {
                "qid": f"d{i:03d}",
                "query": cand["query"],
                "relevant": cand["relevant"],
                "original_gold": cand["original_gold"],
                "intent": cand["intent"],
                "split": "dev",
                "thread_id": cand["thread_id"],
            }
        )
    return dev_split


def assert_disjointness(
    test_split: List[Dict[str, Any]],
    dev_split: List[Dict[str, Any]],
    example_thread_ids: Set[str],
) -> None:
    """Verify strict disjointness between test, dev, and theme examples."""
    test_tids = {item["thread_id"] for item in test_split}
    dev_tids = {item["thread_id"] for item in dev_split}

    overlap_td = test_tids & dev_tids
    assert not overlap_td, f"Disjointness violation: {len(overlap_td)} thread_ids in BOTH dev and test!"

    test_queries = {item["query"] for item in test_split}
    dev_queries = {item["query"] for item in dev_split}
    overlap_q = test_queries & dev_queries
    assert not overlap_q, f"Disjointness violation: {len(overlap_q)} queries in BOTH dev and test!"

    overlap_test_ex = test_tids & example_thread_ids
    assert (
        not overlap_test_ex
    ), f"Leakage violation: {len(overlap_test_ex)} TEST threads in theme examples!"

    overlap_dev_ex = dev_tids & example_thread_ids
    assert not overlap_dev_ex, f"Leakage violation: {len(overlap_dev_ex)} DEV threads in theme examples!"


def write_jsonl(records: List[Dict[str, Any]], out_path: pathlib.Path) -> None:
    """Write list of records to jsonl file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build frozen TEST and disjoint DEV splits.")
    ap.add_argument(
        "--themes",
        default="rag/results/themes_v1.json",
        help="Path to themes_v1.json (default: rag/results/themes_v1.json)",
    )
    ap.add_argument(
        "--threads",
        default=str(DEFAULT_SCRATCH / "threads.jsonl"),
        help="Path to threads.jsonl",
    )
    ap.add_argument(
        "--import",
        dest="imp",
        default=str(DEFAULT_SCRATCH / "faq/faq_articles_import.jsonl"),
        help="Path to faq_articles_import.jsonl",
    )
    ap.add_argument(
        "--gold-map",
        default=str(DEFAULT_SCRATCH / "faq_v2/gold_map_v1_v2.json"),
        help="Path to gold_map_v1_v2.json",
    )
    ap.add_argument(
        "--test-source",
        default=str(DEFAULT_SCRATCH / "ret_v2b_lowest_clean.json"),
        help="Path to ret_v2b_lowest_clean.json containing frozen TEST rows",
    )
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_SCRATCH / "eval"),
        help="Output directory for dev.jsonl and test.jsonl",
    )
    ap.add_argument("--seed", type=int, default=133, help="Seed for DEV split sampling (default: 133)")
    ap.add_argument("--n-dev", type=int, default=300, help="Number of DEV queries (default: 300)")
    ap.add_argument(
        "--stratified",
        action="store_true",
        help="Stratify DEV intent mix to match TEST exactly",
    )
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out_dir)

    # 1. Load inputs
    print(f"Loading themes from {args.themes}...")
    themes = json.load(open(args.themes))
    print(f"Loading threads from {args.threads}...")
    threads = {str(json.loads(l)["thread_id"]): json.loads(l) for l in open(args.threads)}
    print(f"Loading live import IDs from {args.imp}...")
    live_doc_ids = {json.loads(l)["id"] for l in open(args.imp)}
    gold_map = json.load(open(args.gold_map)) if pathlib.Path(args.gold_map).exists() else None

    # 2. Extract candidates
    cands, example_thread_ids = extract_theme_candidates(themes, threads, live_doc_ids, gold_map)
    print(
        f"Extracted {len(cands)} total candidate queries from {len(themes)} themes "
        f"({len(example_thread_ids)} example threads excluded)."
    )

    # 3. Build TEST split
    print(f"Building TEST split from {args.test_source}...")
    test_rows = json.load(open(args.test_source))["rows"]
    test_split = build_test_split(test_rows, cands)
    test_tids = {item["thread_id"] for item in test_split}
    assert len(test_split) == 300, f"Expected 300 TEST queries, got {len(test_split)}"

    # 4. Filter available pool for DEV
    available_for_dev = [c for c in cands if c["thread_id"] not in test_tids]
    print(f"Available pool for DEV: {len(available_for_dev)} candidates.")

    # 5. Build DEV split
    test_counts = Counter(item["intent"] for item in test_split)
    strat_counts = dict(test_counts) if args.stratified else None
    dev_split = build_dev_split(
        available_cands=available_for_dev,
        n_dev=args.n_dev,
        seed=args.seed,
        stratified_counts=strat_counts,
    )
    assert len(dev_split) == args.n_dev, f"Expected {args.n_dev} DEV queries, got {len(dev_split)}"

    # 6. Assert strict disjointness
    assert_disjointness(test_split, dev_split, example_thread_ids)
    print("Disjointness verified: 0 overlapping thread_ids, 0 overlapping queries, 0 example leakage.")

    # 7. Write outputs
    test_out = out_dir / "test.jsonl"
    dev_out = out_dir / "dev.jsonl"
    write_jsonl(test_split, test_out)
    write_jsonl(dev_split, dev_out)
    print(f"Wrote {len(test_split)} TEST records -> {test_out}")
    print(f"Wrote {len(dev_split)} DEV records -> {dev_out}")

    # 8. Report per-intent breakdown
    dev_counts = Counter(item["intent"] for item in dev_split)
    all_intents = sorted(set(test_counts.keys()) | set(dev_counts.keys()))

    print("\n--- Evaluation Splits Per-Intent Counts ---")
    print(f"{'Intent Family':<25} {'TEST':<10} {'DEV':<10} {'Delta':<10}")
    print("-" * 55)
    for intent in all_intents:
        tc = test_counts.get(intent, 0)
        dc = dev_counts.get(intent, 0)
        delta = dc - tc
        sign = "+" if delta > 0 else ""
        print(f"{intent:<25} {tc:<10} {dc:<10} {sign + str(delta):<10}")
    print("-" * 55)
    print(f"{'Total':<25} {len(test_split):<10} {len(dev_split):<10} {0:<10}")


if __name__ == "__main__":
    main()
