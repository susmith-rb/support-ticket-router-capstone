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
5. managed_rerank: pageSize=20 + Ranking API -> top 5
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
import json
import pathlib
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals import eval_metrics
from ticket_router.config.models import model_id
from ticket_router.retrieval.search_client import (
    DEFAULT_DATA_STORE,
    DEFAULT_ENGINE,
    DEFAULT_LOCATION,
    DEFAULT_PROJECT,
    VALID_INTENT_FAMILIES,
    SearchClient,
    SearchResult,
    clean_query,
    load_rich_faq_records,
)

DEFAULT_REWRITE_CACHE_PATH = pathlib.Path(
    "/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/rewrite_cache_dev.jsonl"
)

ALL_CONFIG_NAMES = [
    # Baseline & Threshold
    "baseline",
    "thresh_low_clean",
    "thresh_lowest_noclean",
    "rank_expr_semantic",
    "managed_rerank",
    # Hard intent filters
    "intent_hard_oracle",
    "intent_hard_noisy",
    # Soft intent boosts (oracle)
    "intent_soft_oracle_0.1",
    "intent_soft_oracle_0.2",
    "intent_soft_oracle_0.3",
    "intent_soft_oracle_0.5",
    "intent_soft_oracle_0.8",
    # Soft intent boosts (noisy)
    "intent_soft_noisy_0.1",
    "intent_soft_noisy_0.2",
    "intent_soft_noisy_0.3",
    "intent_soft_noisy_0.5",
    "intent_soft_noisy_0.8",
    # Phase D: Intent boost alone
    "intent_noisy_0.0",
    "intent_noisy_0.1",
    "intent_noisy_0.2",
    # Phase D: Rich record reranking
    "rerank_all_20",
    "rerank_blend_0.05",
    "rerank_blend_0.1",
    "rerank_blend_0.2",
    # Phase D: Query rewrite alone
    "rewrite_only",
    "rewrite_plus_original",
    # Phase D: Combinations
    "rewrite_plus_original_intent_0.2",
    "rewrite_plus_original_rerank_all_20",
    "rewrite_plus_original_rerank_blend_0.05",
    "rewrite_plus_original_rerank_blend_0.1",
    "rewrite_plus_original_rerank_blend_0.2",
    "rewrite_plus_original_intent_0.2_rerank_blend_0.05",
    "rewrite_plus_original_intent_0.2_rerank_blend_0.1",
    "rewrite_plus_original_intent_0.2_rerank_blend_0.2",
    "rewrite_plus_original_intent_0.2_rerank_all_20",
    "rewrite_only_intent_0.2",
    "rewrite_only_rerank_all_20",
    "rewrite_only_rerank_blend_0.1",
    "rewrite_only_intent_0.2_rerank_blend_0.1",
    "intent_noisy_0.2_rerank_blend_0.05",
    "intent_noisy_0.2_rerank_blend_0.1",
    "intent_noisy_0.2_rerank_blend_0.2",
    "intent_noisy_0.2_rerank_all_20",
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
    rerank_model: str = ""
    rerank_top_n: int = 5
    rerank_margin: Optional[float] = None
    use_rich_records: bool = True
    query_rewrite_mode: str = "off"  # "off", "rewrite_only", "rewrite_plus_original"
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
            description="Managed Rerank (pageSize=20 + Ranking API -> top 5)",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_model=model_id("ranking_api_model"),
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
        # Phase D: Intent boost alone
        AblationConfig(
            name="intent_noisy_0.0",
            description="Intent Boost 0.0 with 14% Noisy Router",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.0,
        ),
        AblationConfig(
            name="intent_noisy_0.1",
            description="Intent Boost 0.1 with 14% Noisy Router",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.1,
        ),
        AblationConfig(
            name="intent_noisy_0.2",
            description="Intent Boost 0.2 with 14% Noisy Router",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
        ),
        # Phase D: Rich record reranking alone
        AblationConfig(
            name="rerank_all_20",
            description="Rerank Top 20 Candidates with Rich Records",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=None,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rerank_blend_0.05",
            description="Rerank Top 20 with Rich Records + Blend Margin 0.05",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.05,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rerank_blend_0.1",
            description="Rerank Top 20 with Rich Records + Blend Margin 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rerank_blend_0.2",
            description="Rerank Top 20 with Rich Records + Blend Margin 0.20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.20,
            use_rich_records=True,
        ),
        # Phase D: Query rewrite alone
        AblationConfig(
            name="rewrite_only",
            description="Query Rewrite Only",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_only",
        ),
        AblationConfig(
            name="rewrite_plus_original",
            description="Query Rewrite + Original Query Concatenated",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_plus_original",
        ),
        # Phase D: Combinations
        AblationConfig(
            name="rewrite_plus_original_intent_0.2",
            description="Rewrite + Original with Soft Intent Boost 0.2 (14% Noisy)",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_plus_original",
        ),
        AblationConfig(
            name="rewrite_plus_original_rerank_all_20",
            description="Rewrite + Original with Rerank All 20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=None,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_rerank_blend_0.05",
            description="Rewrite + Original with Rerank Blend 0.05",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.05,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_rerank_blend_0.1",
            description="Rewrite + Original with Rerank Blend 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_rerank_blend_0.2",
            description="Rewrite + Original with Rerank Blend 0.20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.20,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_intent_0.2_rerank_blend_0.05",
            description="Rewrite + Original + Intent 0.2 + Rerank Blend 0.05",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.05,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_intent_0.2_rerank_blend_0.1",
            description="Rewrite + Original + Intent 0.2 + Rerank Blend 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_intent_0.2_rerank_blend_0.2",
            description="Rewrite + Original + Intent 0.2 + Rerank Blend 0.20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.20,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_plus_original_intent_0.2_rerank_all_20",
            description="Rewrite + Original + Intent 0.2 + Rerank All 20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_plus_original",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=None,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_only_intent_0.2",
            description="Rewrite Only + Soft Intent Boost 0.2 (14% Noisy)",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_only",
        ),
        AblationConfig(
            name="rewrite_only_rerank_all_20",
            description="Rewrite Only + Rerank All 20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_only",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=None,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_only_rerank_blend_0.1",
            description="Rewrite Only + Rerank Blend 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            query_rewrite_mode="rewrite_only",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="rewrite_only_intent_0.2_rerank_blend_0.1",
            description="Rewrite Only + Intent 0.2 + Rerank Blend 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            query_rewrite_mode="rewrite_only",
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="intent_noisy_0.2_rerank_blend_0.05",
            description="Intent Boost 0.2 + Rerank Blend 0.05",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.05,
            use_rich_records=True,
        ),
        AblationConfig(
            name="intent_noisy_0.2_rerank_blend_0.1",
            description="Intent Boost 0.2 + Rerank Blend 0.10",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.10,
            use_rich_records=True,
        ),
        AblationConfig(
            name="intent_noisy_0.2_rerank_blend_0.2",
            description="Intent Boost 0.2 + Rerank Blend 0.20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=0.20,
            use_rich_records=True,
        ),
        AblationConfig(
            name="intent_noisy_0.2_rerank_all_20",
            description="Intent Boost 0.2 + Rerank All 20",
            threshold="LOWEST",
            clean=True,
            page_size=20,
            intent_mode="noisy",
            intent_boost=0.2,
            rerank=True,
            rerank_top_n=20,
            rerank_margin=None,
            use_rich_records=True,
        ),
    ]

    if config_names:
        expanded_names = []
        phase_d_set = [
            "baseline",
            "intent_noisy_0.0",
            "intent_noisy_0.1",
            "intent_noisy_0.2",
            "rerank_all_20",
            "rerank_blend_0.05",
            "rerank_blend_0.1",
            "rerank_blend_0.2",
            "rewrite_only",
            "rewrite_plus_original",
            "rewrite_plus_original_intent_0.2",
            "rewrite_plus_original_rerank_all_20",
            "rewrite_plus_original_rerank_blend_0.05",
            "rewrite_plus_original_rerank_blend_0.1",
            "rewrite_plus_original_rerank_blend_0.2",
            "rewrite_plus_original_intent_0.2_rerank_blend_0.05",
            "rewrite_plus_original_intent_0.2_rerank_blend_0.1",
            "rewrite_plus_original_intent_0.2_rerank_blend_0.2",
            "rewrite_plus_original_intent_0.2_rerank_all_20",
            "rewrite_only_intent_0.2",
            "rewrite_only_rerank_all_20",
            "rewrite_only_rerank_blend_0.1",
            "rewrite_only_intent_0.2_rerank_blend_0.1",
            "intent_noisy_0.2_rerank_blend_0.05",
            "intent_noisy_0.2_rerank_blend_0.1",
            "intent_noisy_0.2_rerank_blend_0.2",
            "intent_noisy_0.2_rerank_all_20",
        ]
        for c in config_names:
            if c == "phase_d":
                expanded_names.extend(phase_d_set)
            else:
                expanded_names.append(c)
        selected_set = set(expanded_names)
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


def build_search_kwargs(
    cfg: AblationConfig,
    query_item: Dict[str, Any],
    noisy_intent: Optional[str] = None,
    rewrite_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct keyword arguments for SearchClient.search based on an AblationConfig."""
    qid = (
        query_item.get("qid")
        or query_item.get("query_id")
        or query_item.get("id")
        or str(query_item.get("thread_id", ""))
    )
    qtext = query_item.get("query") or query_item.get("text") or ""
    oracle_intent = query_item.get("intent", "other")

    if cfg.intent_mode == "oracle":
        active_intent = oracle_intent
    elif cfg.intent_mode == "noisy":
        active_intent = noisy_intent
    else:
        active_intent = None

    if cfg.query_rewrite_mode == "rewrite_only" and rewrite_cache and qid in rewrite_cache:
        effective_q = rewrite_cache[qid].get("rewritten", qtext)
    elif cfg.query_rewrite_mode == "rewrite_plus_original" and rewrite_cache and qid in rewrite_cache:
        rw = rewrite_cache[qid].get("rewritten", qtext)
        cl_orig = clean_query(qtext)
        effective_q = f"{rw} {cl_orig}".strip()
    else:
        effective_q = qtext

    return {
        "query": effective_q,
        "page_size": cfg.page_size,
        "threshold": cfg.threshold,
        "clean": cfg.clean,
        "intent_family": active_intent,
        "intent_boost": cfg.intent_boost,
        "fallback_min_results": cfg.fallback_min_results,
        "ranking_expression": cfg.ranking_expression,
        "rerank": False,
        "token": token,
    }


def evaluate_single_config(
    cfg: AblationConfig,
    queries: List[Dict[str, Any]],
    noisy_intents: List[str],
    gold_map: Dict[str, Any],
    client: Any,
    baseline_cache: Optional[Dict[str, SearchResult]] = None,
    rich_records: Optional[Dict[str, Dict[str, str]]] = None,
    rewrite_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    search_cache: Optional[Dict[Tuple[Any, ...], SearchResult]] = None,
    rank_cache: Optional[Dict[Tuple[Any, ...], Tuple[List[Dict[str, Any]], float]]] = None,
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
            "rows": [],
        }

    rows = []
    fallbacks_triggered = 0
    try:
        tok = client.get_token()
    except AttributeError:
        tok = None

    if search_cache is None:
        search_cache = {}
    if rank_cache is None:
        rank_cache = {}

    for idx, item in enumerate(queries):
        if (idx + 1) % 50 == 0 or idx == len(queries) - 1:
            print(f"  [{idx + 1:03d}/{len(queries)}] processing queries...", flush=True)

        qid = item["qid"]
        qtext = item["query"]
        doc = item["relevant"]
        oracle_intent = item.get("intent", "other")
        noisy_intent = noisy_intents[idx]

        target = gold_map.get(doc, doc) if gold_map else doc

        # 1. Base search execution (with search_cache reuse across configs)
        search_kwargs = build_search_kwargs(
            cfg=cfg,
            query_item=item,
            noisy_intent=noisy_intent,
            rewrite_cache=rewrite_cache,
            token=tok,
        )
        effective_q = search_kwargs["query"]
        search_key = (
            search_kwargs["query"],
            search_kwargs["intent_family"],
            search_kwargs["intent_boost"],
            search_kwargs["threshold"],
            search_kwargs["clean"],
            search_kwargs["page_size"],
            search_kwargs["fallback_min_results"],
            search_kwargs["ranking_expression"],
        )

        if search_key in search_cache:
            base_res = search_cache[search_key]
        else:
            base_res = client.search(**search_kwargs)
            search_cache[search_key] = base_res

        if cfg.name == "baseline" and baseline_cache is not None:
            baseline_cache[qid] = base_res

        # 2. Reranking (if enabled)
        if cfg.rerank and base_res.raw_results:
            top_candidates = base_res.raw_results[:cfg.rerank_top_n]
            cand_ids = tuple(r["document"]["id"] for r in top_candidates if "document" in r and "id" in r["document"])
            effective_rerank_model = cfg.rerank_model or model_id("ranking_api_model")
            rank_key = (effective_q, cand_ids, effective_rerank_model, cfg.use_rich_records, cfg.rerank_top_n)

            if rank_key in rank_cache:
                reranked_records, r_lat = rank_cache[rank_key]
            else:
                records = []
                for r in top_candidates:
                    d = r.get("document", {})
                    d_id = d.get("id")
                    if not d_id:
                        continue
                    if cfg.use_rich_records and rich_records and d_id in rich_records:
                        rec = rich_records[d_id]
                        records.append({
                            "id": d_id,
                            "title": rec.get("title", d_id),
                            "content": rec.get("content", ""),
                        })
                    else:
                        derived = d.get("derivedStructData", {})
                        struct = d.get("structData", {})
                        title = derived.get("title") or struct.get("title") or d_id
                        snippets = derived.get("snippets", [])
                        snippet_text = snippets[0].get("snippet", "") if snippets else ""
                        content = snippet_text or title
                        records.append({"id": d_id, "title": title, "content": content})

                can_rank = False
                try:
                    _ = client.rank
                    can_rank = True
                except AttributeError:
                    can_rank = False

                if records and can_rank:
                    try:
                        reranked_records, r_lat = client.rank(
                            query=clean_query(effective_q) if cfg.clean else effective_q,
                            records=records,
                            top_n=cfg.rerank_top_n,
                            model=effective_rerank_model,
                            token=tok,
                        )
                    except Exception:
                        reranked_records = [{"id": cid, "score": 0.0} for cid in cand_ids]
                        r_lat = 0.0
                else:
                    reranked_records = [{"id": cid, "score": 0.0} for cid in cand_ids]
                    r_lat = 0.0
                rank_cache[rank_key] = (reranked_records, r_lat)

            reranked_ids = [rec["id"] for rec in reranked_records if "id" in rec]
            scores = {rec["id"]: rec.get("score", 0.0) for rec in reranked_records if "id" in rec}

            if cfg.rerank_margin is not None and base_res.document_ids:
                original_top1 = base_res.document_ids[0]
                rerank_top1 = reranked_ids[0] if reranked_ids else original_top1
                best_score = scores.get(rerank_top1, 0.0)
                orig_score = scores.get(original_top1, 0.0)
                if (best_score - orig_score) > cfg.rerank_margin:
                    doc_ids = reranked_ids
                else:
                    doc_ids = [original_top1] + [rid for rid in reranked_ids if rid != original_top1]
            else:
                doc_ids = reranked_ids

            total_lat = round(base_res.latency_ms + r_lat, 2)
        else:
            doc_ids = base_res.document_ids
            total_lat = base_res.latency_ms

        rank = eval_metrics.compute_rank(target, doc_ids)
        if base_res.fallback_triggered:
            fallbacks_triggered += 1

        rows.append({
            "qid": qid,
            "query": qtext,
            "top10": doc_ids[:10],
            "top10_ids": doc_ids[:10],
            "latency": total_lat,
            "latency_ms": total_lat,
            "applied_intent": search_kwargs["intent_family"],
            "gold": target,
            "relevant": doc,
            "original_gold": item.get("original_gold", doc),
            "intent": oracle_intent,
            "rank": rank,
            "top3": doc_ids[:3],
            "top_k": doc_ids,
            "fallback_triggered": base_res.fallback_triggered,
            "filter_applied": base_res.filter_applied,
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
        "rows": rows,
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
        "--rewrite-cache",
        type=str,
        default=str(DEFAULT_REWRITE_CACHE_PATH),
        help="Path to rewrite_cache_dev.jsonl",
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

    # Load rich FAQ records for reranking
    rich_records = load_rich_faq_records()
    print(f"Loaded {len(rich_records)} rich FAQ records for reranking.")

    # Load query rewrite cache if present
    rewrite_cache: Dict[str, Dict[str, Any]] = {}
    if args.rewrite_cache and pathlib.Path(args.rewrite_cache).exists():
        with open(args.rewrite_cache, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    rewrite_cache[item["qid"]] = item
        print(f"Loaded {len(rewrite_cache)} query rewrites from: {args.rewrite_cache}")

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
    search_cache: Dict[Tuple[Any, ...], SearchResult] = {}
    rank_cache: Dict[Tuple[Any, ...], Tuple[List[Dict[str, Any]], float]] = {}

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
            rich_records=rich_records,
            rewrite_cache=rewrite_cache,
            search_cache=search_cache,
            rank_cache=rank_cache,
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
