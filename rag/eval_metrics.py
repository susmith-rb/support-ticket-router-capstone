#!/usr/bin/env python3
"""eval_metrics.py — Pure evaluation metrics and slice helpers for RAG retrieval."""

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union


def compute_rank(
    target: Union[str, Sequence[str], Set[str]], retrieved_ids: Sequence[str]
) -> Optional[int]:
    """Return 1-based rank of target (or first match of multiple targets) in retrieved_ids, or None."""
    if isinstance(target, (list, tuple, set)):
        ranks = [retrieved_ids.index(t) + 1 for t in target if t in retrieved_ids]
        return min(ranks) if ranks else None
    return retrieved_ids.index(target) + 1 if target in retrieved_ids else None


def recall_at_k(ranks: Sequence[Optional[int]], k: int, n: Optional[int] = None) -> float:
    """Compute recall@k for a sequence of 1-based ranks (or None)."""
    total = n if n is not None else len(ranks)
    if total <= 0:
        return 0.0
    hits = sum(1 for r in ranks if r is not None and r <= k)
    return hits / total


def mrr_at_k(ranks: Sequence[Optional[int]], k: int = 10, n: Optional[int] = None) -> float:
    """Compute Mean Reciprocal Rank truncated at rank k."""
    total = n if n is not None else len(ranks)
    if total <= 0:
        return 0.0
    reciprocal_sum = sum(1.0 / r for r in ranks if r is not None and r <= k)
    return reciprocal_sum / total


def ndcg_for_rank(rank: Optional[int], k: int = 5) -> float:
    """Compute binary single-gold nDCG@k for a single 1-based rank.

    With single-gold binary relevance, IDCG@k = 1/log2(1+1) = 1.0 for all k >= 1.
    If the relevant doc is at rank r <= k, DCG@k = 1/log2(r+1), so nDCG@k = 1/log2(r+1).
    Rank 1 -> 1.0
    Rank 2 -> 1 / log2(3) ≈ 0.6309
    Rank 3 -> 1 / log2(4) = 0.5
    Rank > k or None -> 0.0
    """
    if rank is None or rank <= 0 or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def ndcg_at_k(ranks: Sequence[Optional[int]], k: int = 5, n: Optional[int] = None) -> float:
    """Compute Mean nDCG@k across a sequence of 1-based ranks (single-gold binary relevance)."""
    total = n if n is not None else len(ranks)
    if total <= 0:
        return 0.0
    ndcg_sum = sum(ndcg_for_rank(r, k=k) for r in ranks)
    return ndcg_sum / total


def latency_percentiles(latencies_ms: Sequence[float]) -> Dict[str, Optional[float]]:
    """Compute count, mean, min, max, p50, p90, p95, p99 from a sequence of latencies in ms."""
    if not latencies_ms:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "min": None,
            "max": None,
        }
    sorted_lats = sorted(latencies_ms)
    n = len(sorted_lats)

    def percentile(p: float) -> float:
        k = (n - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return float(sorted_lats[int(k)])
        d = k - f
        return float(sorted_lats[int(f)] * (1.0 - d) + sorted_lats[int(c)] * d)

    return {
        "count": n,
        "mean": round(sum(sorted_lats) / n, 2),
        "min": round(sorted_lats[0], 2),
        "max": round(sorted_lats[-1], 2),
        "p50": round(percentile(50.0), 2),
        "p90": round(percentile(90.0), 2),
        "p95": round(percentile(95.0), 2),
        "p99": round(percentile(99.0), 2),
    }


def get_dropped_and_merged_ids(gold_map: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """Extract dropped IDs and merged source IDs from gold_map."""
    dropped_ids = set(gold_map.get("dropped", {}).keys())
    mapping = gold_map.get("map", {})
    if not mapping:
        mapping = {k: v for k, v in gold_map.items() if k != "dropped" and isinstance(v, str)}
    merged_ids = {old_id for old_id, new_id in mapping.items() if old_id != new_id}
    return dropped_ids, merged_ids


def filter_slice(
    rows: Sequence[Dict[str, Any]],
    gold_map: Dict[str, Any],
    slice_name: str,
    original_gold_key: str = "original_gold",
    fallback_gold_key: str = "relevant",
) -> List[Dict[str, Any]]:
    """Filter rows into slice (a), (b), or (c) based on gold_map dropped/merged IDs.

    - 'a' or 'a_all': returns all rows
    - 'b' or 'b_excl_dropped': excludes queries where original gold is in gold_map['dropped']
    - 'c' or 'c_excl_dropped_and_merged': excludes dropped AND merged original gold IDs
    """
    if slice_name in ("a", "a_all", "all"):
        return list(rows)

    dropped_ids, merged_ids = get_dropped_and_merged_ids(gold_map)

    def get_gold(row: Dict[str, Any]) -> str:
        return row.get(original_gold_key) or row.get(fallback_gold_key, "")

    if slice_name in ("b", "b_excl_dropped", "gate"):
        return [r for r in rows if get_gold(r) not in dropped_ids]

    if slice_name in ("c", "c_excl_dropped_and_merged", "clean"):
        return [r for r in rows if get_gold(r) not in dropped_ids and get_gold(r) not in merged_ids]

    raise ValueError(f"Unknown slice_name: '{slice_name}'. Expected 'a', 'b', or 'c'.")


def compute_metrics_summary(
    rows: Sequence[Dict[str, Any]],
    rank_key: str = "rank",
    intent_key: str = "intent",
    latency_key: Optional[str] = "latency_ms",
) -> Dict[str, Any]:
    """Compute complete retrieval metric summary over a list of evaluated rows."""
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "zero_result_queries": 0,
            "recall@1": 0.0,
            "recall@3": 0.0,
            "recall@5": 0.0,
            "recall@10": 0.0,
            "mrr@10": 0.0,
            "ndcg@5": 0.0,
            "by_intent": {},
        }
    ranks = [r.get(rank_key) for r in rows]

    res: Dict[str, Any] = {
        "n": n,
        "zero_result_queries": sum(
            1 for r in rows if not r.get("top3") and not r.get("top_k") and not r.get("top")
        ),
        "recall@1": recall_at_k(ranks, 1, n),
        "recall@3": recall_at_k(ranks, 3, n),
        "recall@5": recall_at_k(ranks, 5, n),
        "recall@10": recall_at_k(ranks, 10, n),
        "mrr@10": mrr_at_k(ranks, 10, n),
        "ndcg@5": ndcg_at_k(ranks, 5, n),
    }

    if latency_key:
        lats = [r[latency_key] for r in rows if r.get(latency_key) is not None]
        if lats:
            res["latency_ms"] = latency_percentiles(lats)

    # Intent breakdown
    by_intent: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        intent = r.get(intent_key, "unknown")
        by_intent.setdefault(intent, []).append(r)

    res["by_intent"] = {}
    for intent, i_rows in sorted(by_intent.items()):
        i_ranks = [r.get(rank_key) for r in i_rows]
        i_n = len(i_rows)
        res["by_intent"][intent] = {
            "n": i_n,
            "recall@1": recall_at_k(i_ranks, 1, i_n),
            "recall@3": recall_at_k(i_ranks, 3, i_n),
            "recall@5": recall_at_k(i_ranks, 5, i_n),
            "recall@10": recall_at_k(i_ranks, 10, i_n),
            "mrr@10": mrr_at_k(i_ranks, 10, i_n),
            "ndcg@5": ndcg_at_k(i_ranks, 5, i_n),
        }
    return res
