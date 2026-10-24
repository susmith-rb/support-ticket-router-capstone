#!/usr/bin/env python3
"""ablation_eval.py — Retrieval ablation evaluation framework on held-out DEV set.

Executes and benchmarks ablation configurations against the held-out DEV query set
($S/eval/dev.jsonl). Strictly enforces split discipline: evaluation against TEST
is blocked.

Supported Ablation Grid:
1. baseline: threshold=LOWEST, clean=True (current production)
2. thresh_low_clean: threshold=LOW, clean=True
3. thresh_lowest_noclean: threshold=LOWEST, clean=False
4. rank_expr_semantic: rankingExpression="semantic_similarity_score" (marked unsupported)
5. managed_rerank: pageSize=20 + Ranking API (semantic-ranker-512@latest) -> top 5
6. intent_hard_oracle: Hard filter intent_family: ANY("<oracle>") with unconstrained fallback
7. intent_hard_noisy: Hard filter with 14% noisy intent corruption with fallback
8. intent_soft_oracle_0.2: Soft boost 0.2 with oracle intent
9. intent_soft_oracle_0.5: Soft boost 0.5 with oracle intent
10. intent_soft_oracle_0.8: Soft boost 0.8 with oracle intent
11. intent_soft_noisy_0.2: Soft boost 0.2 with 14% noisy intent
12. intent_soft_noisy_0.5: Soft boost 0.5 with 14% noisy intent
13. intent_soft_noisy_0.8: Soft boost 0.8 with 14% noisy intent
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import pathlib
import random
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import eval_metrics
from search_client import (
    DEFAULT_DATA_STORE,
    DEFAULT_ENGINE,
    DEFAULT_LOCATION,
    DEFAULT_PROJECT,
    SearchResult,
    SearchClient,
    VALID_INTENT_FAMILIES,
)

ALL_CONFIG_NAMES = [
    "baseline",
    "thresh_low_clean",
    "thresh_lowest_noclean",
    "rank_expr_semantic",
    "managed_rerank",
    "intent_hard_oracle",
    "intent_hard_noisy",
    "intent_soft_oracle_0.1",
    "intent_soft_oracle_0.2",
    "intent_soft_oracle_0.3",
    "intent_soft_oracle_0.5",
    "intent_soft_oracle_0.8",
    "intent_soft_noisy_0.1",
    "intent_soft_noisy_0.2",
    "intent_soft_noisy_0.3",
    "intent_soft_noisy_0.5",
    "intent_soft_noisy_0.8",
]


@dataclass
class AblationConfig:
    name: str
    description: str
    threshold: str = "LOWEST"
    clean: bool = True
    page_size: int = 10
    intent_mode: str = "off"  # "off", "oracle", "noisy"
    intent_boost: Optional[float] = None
    rerank: bool = False
    rerank_model: str = "semantic-ranker-512@latest"
    rerank_top_n: int = 5
    ranking_expression: str = ""
    fallback_min_results: int = 2
    supported: bool = True
    support_note: str = ""


def get_ablation_configs(config_names: Optional[Sequence[str]] = None) -> List[AblationConfig]:
    """Return dictionary/list of standard ablation configurations."""
    configs = [
        AblationConfig(
            name="baseline",
            description="Baseline (relevanceThreshold=LOWEST, clean=True, no intent)",
            threshold="LOWEST",
            clean=True,
            page_size=20,  # fetch 20 so managed_rerank can reuse candidates
            intent_mode="off",
        ),
        AblationConfig(
            name="thresh_low_clean",
            description="Relevance Threshold LOW (API default, clean=True)",
            threshold="LOW",
            clean=True,
            page_size=10,
            intent_mode="off",
        ),
        AblationConfig(
            name="thresh_lowest_noclean",
            description="Raw Queries (threshold=LOWEST, clean=False)",
            threshold="LOWEST",
            clean=False,
            page_size=10,
            intent_mode="off",
        ),
        AblationConfig(
            name="rank_expr_semantic",
            description="rankingExpression='semantic_similarity_score'",
            threshold="LOWEST",
            clean=True,
            ranking_expression="semantic_similarity_score",
            supported=False,
            support_note="HTTP 400: Valid 'embedding_spec' has to be provided along with 'ranking_expression'.",
        ),
        AblationConfig(
            name="managed_rerank",
            description="Managed Rerank (pageSize=20 + semantic-ranker-512@latest -> top 5)",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_model="semantic-ranker-512@latest",
            rerank_top_n=5,
        ),
        AblationConfig(
            name="intent_hard_oracle",
            description="Hard Intent Filter (Oracle) with fallback if < 2 results",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=None,
        ),
        AblationConfig(
            name="intent_hard_noisy",
            description="Hard Intent Filter (14% Noisy Router) with fallback",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=None,
        ),
        AblationConfig(
            name="intent_soft_oracle_0.1",
            description="Soft Intent Boost (Oracle, boost=0.1)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=0.1,
        ),
        AblationConfig(
            name="intent_soft_oracle_0.2",
            description="Soft Intent Boost (Oracle, boost=0.2)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=0.2,
        ),
        AblationConfig(
            name="intent_soft_oracle_0.3",
            description="Soft Intent Boost (Oracle, boost=0.3)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=0.3,
        ),
        AblationConfig(
            name="intent_soft_oracle_0.5",
            description="Soft Intent Boost (Oracle, boost=0.5)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=0.5,
        ),
        AblationConfig(
            name="intent_soft_oracle_0.8",
            description="Soft Intent Boost (Oracle, boost=0.8)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="oracle",
            intent_boost=0.8,
        ),
        AblationConfig(
            name="intent_soft_noisy_0.1",
            description="Soft Intent Boost (14% Noisy Router, boost=0.1)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=0.1,
        ),
        AblationConfig(
            name="intent_soft_noisy_0.2",
            description="Soft Intent Boost (14% Noisy Router, boost=0.2)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=0.2,
        ),
        AblationConfig(
            name="intent_soft_noisy_0.3",
            description="Soft Intent Boost (14% Noisy Router, boost=0.3)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=0.3,
        ),
        AblationConfig(
            name="intent_soft_noisy_0.5",
            description="Soft Intent Boost (14% Noisy Router, boost=0.5)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=0.5,
        ),
        AblationConfig(
            name="intent_soft_noisy_0.8",
            description="Soft Intent Boost (14% Noisy Router, boost=0.8)",
            threshold="LOWEST",
            clean=True,
            page_size=10,
            intent_mode="noisy",
            intent_boost=0.8,
        ),
    ]

    if config_names:
        selected_set = set(config_names)
        return [c for c in configs if c.name in selected_set]
    return configs


def generate_noisy_intents(
    queries: Sequence[Dict[str, Any]], seed: int = 42, error_rate: float = 0.14
) -> List[str]:
    """Generate deterministic noisy intents using standard seed 42."""
    rng = random.Random(seed)
    noisy = []
    for q in queries:
        oracle_intent = q.get("intent", "other")
        if rng.random() < error_rate:
            alts = [fam for fam in VALID_INTENT_FAMILIES if fam != oracle_intent]
            noisy.append(rng.choice(alts) if alts else oracle_intent)
        else:
            noisy.append(oracle_intent)
    return noisy


def evaluate_single_config(
    cfg: AblationConfig,
    queries: List[Dict[str, Any]],
    noisy_intents: List[str],
    gold_map: Dict[str, Any],
    client: Any,
    baseline_cache: Optional[Dict[str, SearchResult]] = None,
) -> Dict[str, Any]:
    """Evaluate a single ablation configuration against the queries."""
    if not cfg.supported:
        return {
            "config": cfg.name,
            "description": cfg.description,
            "status": "unsupported",
            "support_note": cfg.support_note,
            "slice_a": {},
            "slice_b": {},
            "fallbacks_triggered": 0,
        }

    rows = []
    fallbacks_triggered = 0
    tok = client.get_token() if hasattr(client, "get_token") else None

    for idx, item in enumerate(queries):
        if (idx + 1) % 50 == 0 or idx == len(queries) - 1:
            print(f"  [{idx + 1:03d}/{len(queries)}] processing queries...", flush=True)

        qid = item["qid"]
        qtext = item["query"]
        doc = item["relevant"]
        oracle_intent = item.get("intent", "other")
        noisy_intent = noisy_intents[idx]

        target = gold_map.get(doc, doc) if gold_map else doc

        # Select intent family based on intent_mode
        if cfg.intent_mode == "oracle":
            active_intent = oracle_intent
        elif cfg.intent_mode == "noisy":
            active_intent = noisy_intent
        else:
            active_intent = None

        # Check if we can reuse baseline search results for managed_rerank
        res: SearchResult
        if cfg.name == "managed_rerank" and baseline_cache and qid in baseline_cache:
            base_res = baseline_cache[qid]
            # Perform rerank call on base_res raw results
            records = []
            for r in base_res.raw_results[:20]:
                d = r.get("document", {})
                d_id = d.get("id")
                if not d_id:
                    continue
                derived = d.get("derivedStructData", {})
                struct = d.get("structData", {})
                title = derived.get("title") or struct.get("title") or d_id
                snippets = derived.get("snippets", [])
                snippet_text = snippets[0].get("snippet", "") if snippets else ""
                content = snippet_text or title
                records.append({"id": d_id, "title": title, "content": content})

            if records and hasattr(client, "rank"):
                try:
                    reranked, r_lat = client.rank(
                        query=qtext,
                        records=records,
                        top_n=cfg.rerank_top_n,
                        model=cfg.rerank_model,
                        token=tok,
                    )
                    top_ids = [rec["id"] for rec in reranked if "id" in rec]
                    tot_lat = base_res.latency_ms + r_lat
                    res = SearchResult(
                        document_ids=top_ids,
                        latency_ms=round(tot_lat, 2),
                        total_size=base_res.total_size,
                        filter_applied=base_res.filter_applied,
                        fallback_triggered=base_res.fallback_triggered,
                        rerank_applied=True,
                        raw_results=base_res.raw_results,
                    )
                except Exception:
                    res = SearchResult(
                        document_ids=base_res.document_ids[:cfg.rerank_top_n],
                        latency_ms=base_res.latency_ms,
                        total_size=base_res.total_size,
                        filter_applied=base_res.filter_applied,
                        fallback_triggered=base_res.fallback_triggered,
                        rerank_applied=False,
                    )
            else:
                res = SearchResult(
                    document_ids=base_res.document_ids[:cfg.rerank_top_n],
                    latency_ms=base_res.latency_ms,
                    total_size=base_res.total_size,
                    filter_applied=base_res.filter_applied,
                    fallback_triggered=base_res.fallback_triggered,
                    rerank_applied=False,
                )
        else:
            # Execute search
            res = client.search(
                query=qtext,
                page_size=cfg.page_size,
                threshold=cfg.threshold,
                clean=cfg.clean,
                intent_family=active_intent,
                intent_boost=cfg.intent_boost,
                fallback_min_results=cfg.fallback_min_results,
                ranking_expression=cfg.ranking_expression,
                rerank=cfg.rerank,
                rerank_model=cfg.rerank_model,
                rerank_top_n=cfg.rerank_top_n,
                token=tok,
            )

        if cfg.name == "baseline" and baseline_cache is not None:
            baseline_cache[qid] = res

        retrieved_ids = res.document_ids
        rank = eval_metrics.compute_rank(target, retrieved_ids)

        if res.fallback_triggered:
            fallbacks_triggered += 1

        rows.append({
            "qid": qid,
            "query": qtext,
            "gold": target,
            "relevant": doc,
            "original_gold": item.get("original_gold", doc),
            "intent": oracle_intent,
            "rank": rank,
            "top3": retrieved_ids[:3],
            "top_k": retrieved_ids,
            "latency_ms": res.latency_ms,
            "fallback_triggered": res.fallback_triggered,
            "filter_applied": res.filter_applied,
        })

    # Slice (a): all queries
    slice_a_summary = eval_metrics.compute_metrics_summary(rows)

    # Slice (b): fair comparison excluding dropped
    if gold_map and "dropped" in gold_map:
        rows_b = eval_metrics.filter_slice(rows, gold_map, "b")
        slice_b_summary = eval_metrics.compute_metrics_summary(rows_b)
    else:
        slice_b_summary = slice_a_summary

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    lats_summary = eval_metrics.latency_percentiles(latencies)

    return {
        "config": cfg.name,
        "description": cfg.description,
        "status": "completed",
        "fallbacks_triggered": fallbacks_triggered,
        "latency_summary": lats_summary,
        "slice_a": slice_a_summary,
        "slice_b": slice_b_summary,
    }


def generate_markdown_table(results: List[Dict[str, Any]]) -> str:
    """Format ablation results as a clean GitHub-style Markdown table."""
    lines = [
        "| Configuration | Status | Slice (b) n | Recall@1 | Recall@5 | Recall@10 | MRR@10 | nDCG@5 | Latency p50 | Latency p95 | Fallbacks |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    for r in results:
        cfg = r["config"]
        status = r.get("status", "unknown")
        if status != "completed":
            lines.append(f"| **{cfg}** | *{status}* | - | - | - | - | - | - | - | - | - |")
            continue

        b = r.get("slice_b", {})
        n = b.get("n", 0)
        r1 = b.get("recall@1", 0.0)
        r5 = b.get("recall@5", 0.0)
        r10 = b.get("recall@10", 0.0)
        mrr = b.get("mrr@10", 0.0)
        ndcg = b.get("ndcg@5", 0.0)
        fb = r.get("fallbacks_triggered", 0)
        lat = r.get("latency_summary", {})
        p50 = f"{lat.get('p50')} ms" if lat.get("p50") is not None else "-"
        p95 = f"{lat.get('p95')} ms" if lat.get("p95") is not None else "-"

        lines.append(
            f"| **{cfg}** | completed | {n} | {r1:.4f} | **{r5:.4f}** | {r10:.4f} | {mrr:.4f} | {ndcg:.4f} | {p50} | {p95} | {fb} |"
        )
    return "\n".join(lines)


def validate_split_and_config_guard(
    queries_path: str,
    queries: List[Dict[str, Any]],
    final_test: bool = False,
    selected_configs: Optional[List[str]] = None,
    audit_log_path: Optional[str] = None,
) -> None:
    """Enforce split discipline between DEV and TEST sets.

    - If queries are from TEST (path contains 'test.jsonl' or any QID starts with 't'):
      - final_test MUST be True. If False, raises ValueError.
      - An explicit configuration allow-list via --configs MUST be specified.
      - Logs a timestamped audit record to audit_log_path ($S/ablation/test_runs.log).
    """
    path_str = str(queries_path)
    is_test = "test.jsonl" in path_str or any(
        str(q.get("qid", "")).startswith("t") for q in queries
    )
    if is_test:
        if not final_test:
            raise ValueError(
                "ERROR: SPLIT DISCIPLINE VIOLATION! Evaluation on TEST set ($S/eval/test.jsonl) requires --final-test flag."
            )
        if not selected_configs:
            raise ValueError(
                "ERROR: --final-test requires an explicit config allow-list via --configs. Running full ablation grid on TEST is prohibited."
            )
        audit_file = pathlib.Path(
            audit_log_path
            or "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/ablation/test_runs.log"
        )
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        audit_entry = f"{timestamp} | FINAL_TEST_RUN | configs={','.join(selected_configs)} | queries={queries_path} | n={len(queries)}\n"
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(audit_entry)


def main():
    parser = argparse.ArgumentParser(description="Run retrieval ablation eval on DEV set.")
    parser.add_argument(
        "--dev-queries",
        type=str,
        default="/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/eval/dev.jsonl",
        help="Path to dev.jsonl or test.jsonl",
    )
    parser.add_argument(
        "--gold-map",
        type=str,
        default="/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/faq_v2/gold_map_v1_v2.json",
        help="Path to gold_map.json",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/ablation/dev_results.json",
        help="Path to output json file",
    )
    parser.add_argument(
        "--configs",
        type=str,
        default="",
        help="Comma-separated list of configuration names to run (default: all)",
    )
    parser.add_argument(
        "--final-test",
        action="store_true",
        help="Explicit permission flag required to evaluate against the frozen TEST set",
    )
    parser.add_argument(
        "--audit-log",
        type=str,
        default="/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/ablation/test_runs.log",
        help="Path to audit log file for final test runs",
    )
    parser.add_argument(
        "--output-key",
        type=str,
        default=None,
        help="Optional top-level JSON key to store results under in output file",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project")
    parser.add_argument("--engine", default=DEFAULT_ENGINE, help="Discovery Engine search app")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help="Location (global)")
    parser.add_argument("--sleep", type=float, default=0.15, help="Throttle sleep per request (sec)")
    args = parser.parse_args()

    print(f"Loading queries from: {args.dev_queries}")
    with open(args.dev_queries, "r", encoding="utf-8") as f:
        dev_queries = [json.loads(line) for line in f if line.strip()]

    selected_configs = (
        [c.strip() for c in args.configs.split(",") if c.strip()]
        if args.configs
        else None
    )

    # Enforce split discipline guard
    try:
        validate_split_and_config_guard(
            queries_path=args.dev_queries,
            queries=dev_queries,
            final_test=args.final_test,
            selected_configs=selected_configs,
            audit_log_path=args.audit_log,
        )
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        sys.exit(1)

    print(f"Loaded {len(dev_queries)} queries.")

    gold_map = {}
    if args.gold_map and pathlib.Path(args.gold_map).exists():
        with open(args.gold_map, "r", encoding="utf-8") as f:
            gold_map = json.load(f)
        print(f"Loaded gold map from: {args.gold_map}")

    # Generate deterministic noisy intents (seed 42)
    noisy_intents = generate_noisy_intents(dev_queries, seed=42, error_rate=0.14)

    # Initialize SearchClient
    client = SearchClient(
        project=args.project,
        engine=args.engine,
        data_store=DEFAULT_DATA_STORE,
        location=args.location,
        sleep_sec=args.sleep,
    )

    configs = get_ablation_configs(selected_configs)
    print(f"Running {len(configs)} configurations: {[c.name for c in configs]}")

    results = []
    baseline_cache: Dict[str, SearchResult] = {}

    for c_idx, cfg in enumerate(configs, 1):
        print(f"\n[{c_idx}/{len(configs)}] Evaluating config: {cfg.name} ({cfg.description})")
        t0 = time.perf_counter()
        res = evaluate_single_config(
            cfg=cfg,
            queries=dev_queries,
            noisy_intents=noisy_intents,
            gold_map=gold_map,
            client=client,
            baseline_cache=baseline_cache,
        )
        elapsed = time.perf_counter() - t0
        print(f"Completed {cfg.name} in {elapsed:.1f}s. Status: {res.get('status')}")
        if res.get("status") == "completed":
            b = res.get("slice_b", {})
            print(
                f"  Slice (b) Recall@1: {b.get('recall@1', 0.0):.4f} | Recall@5: {b.get('recall@5', 0.0):.4f} | MRR: {b.get('mrr@10', 0.0):.4f} | nDCG@5: {b.get('ndcg@5', 0.0):.4f}"
            )
        results.append(res)

    # Save results
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing_data: Dict[str, Any] = {}
    if out_path.exists():
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception:
            existing_data = {}

    report_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "queries_path": args.dev_queries,
        "n_queries": len(dev_queries),
        "results": results,
    }

    if args.output_key:
        existing_data[args.output_key] = report_data
        to_save = existing_data
    else:
        existing_data.update(report_data)
        to_save = existing_data

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2)
    print(f"\nSaved ablation results to: {out_path} (output_key={args.output_key})")

    # Print markdown table
    md_table = generate_markdown_table(results)
    print("\n" + "=" * 80)
    print("ABLATION RESULTS TABLE")
    print("=" * 80)
    print(md_table)
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
