#!/usr/bin/env python3
"""multi_label_eval.py — TDD-conformant Multi-Relevant Retrieval Measurement & Adjudication Evaluation.

Pre-registered in docs/plans/PLAN4.md (2026-10-24) to evaluate Agent Search
retrieval against multi-annotator ground truth (annotators A, B, and adjudicator C).

Capabilities:
1. Inter-annotator agreement statistics (A vs B):
   - Cohen's kappa on support_request (binary)
   - Exact-match rate on primary
   - Cohen's kappa on primary (categorical)
   - Mean Jaccard overlap of relevant document sets
2. Scope filtering and waterfall exclusion accounting:
   - MAIN scope: support_request == True AND language == "en" AND primary is not None
   - ALL-support scope: support_request == True AND primary is not None
   - Exclusions breakdown: not_support_request, non_english, no_primary
3. Retrieval performance metrics:
   - recall@k (default k=5): hit if any relevant document is retrieved
   - MRR@10: reciprocal rank of the first relevant document
   - Graded nDCG@5: gain=2 for primary, gain=1 for secondary; normalized by query's own IDCG
4. Sensitivity bounds:
   - LOWER: consensus primary doc only ({primary})
   - CONSENSUS: adjudicated C relevant set (primary + secondary)
   - UPPER: union of relevant documents (A ∪ B ∪ C)
5. Per-intent breakdown using consensus primary's intent family prefix:
   - faq-order-status -> order_status
   - faq-account-payment -> account_payment
   - faq-returns-refunds -> returns_refunds
   - faq-shipping-policy -> shipping_policy
   - faq-other -> other
6. Decision rule evaluation against TDD §6.4 targets:
   - recall@5 >= 0.90, MRR@10 >= 0.75, nDCG@5 >= 0.80
"""

import argparse
import collections
from datetime import datetime, timezone
import json
import math
import pathlib
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# Intent family prefix mapping per PLAN4.md
INTENT_PREFIXES = [
    ("faq-order-status", "order_status"),
    ("faq-account-payment", "account_payment"),
    ("faq-returns-refunds", "returns_refunds"),
    ("faq-shipping-policy", "shipping_policy"),
    ("faq-other", "other"),
    ("order_status", "order_status"),
    ("account_payment", "account_payment"),
    ("returns_refunds", "returns_refunds"),
    ("shipping_policy", "shipping_policy"),
    ("other", "other"),
]


def infer_intent_from_doc_id(doc_id: Optional[str]) -> str:
    """Infer intent family from document ID prefix per PLAN4 specification."""
    if not doc_id:
        return "other"
    doc_lower = doc_id.lower()
    for prefix, intent in INTENT_PREFIXES:
        if doc_lower.startswith(prefix):
            return intent
    return "other"


def load_label_file(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Load and normalize a blind labeling JSONL file."""
    if not path:
        return {}
    p = pathlib.Path(path)
    if not p.exists():
        return {}

    labels: Dict[str, Dict[str, Any]] = {}
    with open(p, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                rec = json.loads(line_str)
            except json.JSONDecodeError as e:
                sys.stderr.write(f"Warning: JSON parse error in {path}:{line_num}: {e}\n")
                continue

            qid = str(rec.get("qid", "")).strip()
            if not qid:
                continue

            support_req = bool(rec.get("support_request", False))
            lang = str(rec.get("language", "en")).strip().lower()
            primary = rec.get("primary")
            if primary is not None:
                primary = str(primary).strip()
                if not primary:
                    primary = None

            raw_sec = rec.get("secondary") or []
            secondary = [str(s).strip() for s in raw_sec if s and str(s).strip()]

            raw_rel = rec.get("relevant") or []
            rel_list = [str(s).strip() for s in raw_rel if s and str(s).strip()]

            rel_set: Set[str] = set()
            if primary:
                rel_set.add(primary)
            rel_set.update(secondary)
            rel_set.update(rel_list)

            # If secondary wasn't explicitly provided, infer from relevant list excluding primary
            if not secondary and rel_list:
                secondary = [d for d in rel_list if d != primary]

            labels[qid] = {
                "qid": qid,
                "support_request": support_req,
                "language": lang,
                "primary": primary,
                "secondary": secondary,
                "relevant_set": sorted(list(rel_set)),
                "note": str(rec.get("note", "")),
            }
    return labels


def load_labels(
    path_a: Optional[str] = None,
    path_b: Optional[str] = None,
    path_c: Optional[str] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load annotators A, B, and adjudicator C labels."""
    labels_a = load_label_file(path_a)
    labels_b = load_label_file(path_b)
    labels_c = load_label_file(path_c)
    return labels_a, labels_b, labels_c


def compute_cohen_kappa(rater1: Sequence[Any], rater2: Sequence[Any]) -> float:
    """Compute Cohen's kappa for two raters across N items."""
    if len(rater1) != len(rater2):
        raise ValueError(f"Length mismatch: {len(rater1)} vs {len(rater2)}")
    n = len(rater1)
    if n == 0:
        return 1.0

    # Convert to canonical string representation
    r1 = ["__NONE__" if x is None else str(x) for x in rater1]
    r2 = ["__NONE__" if x is None else str(x) for x in rater2]

    # Observed agreement
    agreements = sum(1 for a, b in zip(r1, r2) if a == b)
    p_o = agreements / n

    # Expected agreement by chance
    counts1 = collections.Counter(r1)
    counts2 = collections.Counter(r2)
    categories = sorted(list(set(r1) | set(r2)))
    p_e = sum((counts1[c] / n) * (counts2[c] / n) for c in categories)

    if math.isclose(p_e, 1.0):
        return 1.0 if math.isclose(p_o, 1.0) else 0.0

    return (p_o - p_e) / (1.0 - p_e)


def compute_agreement_stats(
    labels_a: Dict[str, Dict[str, Any]],
    labels_b: Dict[str, Dict[str, Any]],
    split: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute agreement metrics between annotator A and B on common queries."""
    all_common = set(labels_a.keys()) & set(labels_b.keys())
    if split:
        split_clean = split.lower().strip()
        prefix = "t" if split_clean == "test" else ("d" if split_clean == "dev" else None)
        if prefix:
            all_common = {q for q in all_common if q.startswith(prefix)}

    common_qids = sorted(list(all_common))
    n = len(common_qids)
    if n == 0:
        return {
            "n_common_queries": 0,
            "cohen_kappa_support_request": 0.0,
            "exact_match_primary": 0.0,
            "cohen_kappa_primary": 0.0,
            "mean_jaccard_relevant": 0.0,
        }

    support_a = [labels_a[q]["support_request"] for q in common_qids]
    support_b = [labels_b[q]["support_request"] for q in common_qids]
    kappa_support = compute_cohen_kappa(support_a, support_b)

    primary_a = [labels_a[q]["primary"] for q in common_qids]
    primary_b = [labels_b[q]["primary"] for q in common_qids]
    exact_match = sum(1 for a, b in zip(primary_a, primary_b) if a == b) / n
    kappa_primary = compute_cohen_kappa(primary_a, primary_b)

    jaccards = []
    for q in common_qids:
        s_a = set(labels_a[q]["relevant_set"])
        s_b = set(labels_b[q]["relevant_set"])
        if not s_a and not s_b:
            jaccards.append(1.0)
        elif not s_a or not s_b:
            jaccards.append(0.0)
        else:
            jaccards.append(len(s_a & s_b) / len(s_a | s_b))
    mean_jaccard = sum(jaccards) / n if n > 0 else 0.0

    return {
        "n_common_queries": n,
        "cohen_kappa_support_request": round(kappa_support, 4),
        "exact_match_primary": round(exact_match, 4),
        "cohen_kappa_primary": round(kappa_primary, 4),
        "mean_jaccard_relevant": round(mean_jaccard, 4),
    }


def filter_scope(
    labels: Dict[str, Dict[str, Any]], scope: str = "MAIN"
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Filter queries by scope and compute waterfall exclusion counts.

    Scopes:
    - MAIN: support_request == True AND language == "en" AND primary is not None
    - ALL-support: support_request == True AND primary is not None
    """
    scope_upper = scope.strip().upper()
    in_scope: Dict[str, Dict[str, Any]] = {}
    exclusions = {
        "not_support_request": 0,
        "non_english": 0,
        "no_primary": 0,
        "total_excluded": 0,
        "total_in_scope": 0,
        "total_evaluated": len(labels),
    }

    for qid, rec in labels.items():
        is_support = rec.get("support_request", False)
        lang = rec.get("language", "en")
        primary = rec.get("primary")

        # Waterfall classification
        if not is_support:
            exclusions["not_support_request"] += 1
            exclusions["total_excluded"] += 1
        elif scope_upper == "MAIN" and lang != "en":
            exclusions["non_english"] += 1
            exclusions["total_excluded"] += 1
        elif primary is None:
            exclusions["no_primary"] += 1
            exclusions["total_excluded"] += 1
        else:
            in_scope[qid] = rec

    exclusions["total_in_scope"] = len(in_scope)
    return in_scope, exclusions


def compute_graded_dcg(
    retrieved_ids: Sequence[str],
    primary: Optional[str],
    secondary: Sequence[str],
    k: int = 5,
    exp: bool = False,
) -> float:
    """Compute Discounted Cumulative Gain with graded relevance (primary=2, secondary=1)."""
    dcg = 0.0
    sec_set = set(secondary)
    cutoff = min(k, len(retrieved_ids))
    for i in range(cutoff):
        doc_id = retrieved_ids[i]
        if primary and doc_id == primary:
            gain = 2.0
        elif doc_id in sec_set:
            gain = 1.0
        else:
            gain = 0.0

        if gain > 0.0:
            numerator = (math.pow(2.0, gain) - 1.0) if exp else gain
            discount = math.log2(i + 2)
            dcg += numerator / discount
    return dcg


def compute_graded_idcg(
    primary: Optional[str],
    secondary: Sequence[str],
    k: int = 5,
    exp: bool = False,
) -> float:
    """Compute Ideal DCG derived from query's own label set."""
    gains = []
    if primary:
        gains.append(2.0)
    for _ in secondary:
        gains.append(1.0)
    gains.sort(reverse=True)

    idcg = 0.0
    cutoff = min(k, len(gains))
    for i in range(cutoff):
        gain = gains[i]
        numerator = (math.pow(2.0, gain) - 1.0) if exp else gain
        discount = math.log2(i + 2)
        idcg += numerator / discount
    return idcg


def compute_graded_ndcg(
    retrieved_ids: Sequence[str],
    primary: Optional[str],
    secondary: Sequence[str],
    k: int = 5,
    exp: bool = False,
) -> float:
    """Compute normalized graded DCG@k for a query."""
    idcg = compute_graded_idcg(primary, secondary, k=k, exp=exp)
    if idcg <= 0.0:
        return 0.0
    dcg = compute_graded_dcg(retrieved_ids, primary, secondary, k=k, exp=exp)
    return dcg / idcg


def compute_query_retrieval_metrics(
    retrieved_ids: Sequence[str],
    relevant_set: Set[str],
    primary: Optional[str],
    secondary: Sequence[str],
    k_recall: int = 5,
    k_mrr: int = 10,
    k_ndcg: int = 5,
) -> Dict[str, float]:
    """Compute retrieval metrics for a single query given retrieved candidates."""
    # Recall@k
    top_recall = retrieved_ids[:k_recall]
    recall = 1.0 if any(d in relevant_set for d in top_recall) else 0.0

    # MRR@k
    mrr = 0.0
    cutoff_mrr = min(k_mrr, len(retrieved_ids))
    for rank_idx in range(cutoff_mrr):
        if retrieved_ids[rank_idx] in relevant_set:
            mrr = 1.0 / (rank_idx + 1)
            break

    # Graded nDCG@k
    ndcg = compute_graded_ndcg(retrieved_ids, primary, secondary, k=k_ndcg)

    return {
        "recall": recall,
        "mrr": mrr,
        "ndcg": ndcg,
    }


def load_system_rows(
    system_run_path: str, config_name: Optional[str] = None
) -> Dict[str, List[str]]:
    """Extract mapping of qid -> top10 retrieved doc IDs from system run JSON file."""
    p = pathlib.Path(system_run_path)
    if not p.exists():
        raise FileNotFoundError(f"System run file not found: {system_run_path}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Direct dict mapping qid -> list of ids
    if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
        return {str(k): [str(x) for x in v] for k, v in data.items()}

    # 2. Extract results list from data or nested key
    results = None
    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            results = data["results"]
        else:
            # Check nested keys like "finalists_test"
            for val in data.values():
                if isinstance(val, dict) and "results" in val and isinstance(val["results"], list):
                    results = val["results"]
                    break

    # 3. If data itself is a list of rows
    if isinstance(data, list):
        rows = data
    elif results is not None:
        target_res = None
        if config_name:
            for r in results:
                if isinstance(r, dict) and r.get("config") == config_name:
                    target_res = r
                    break
            if not target_res:
                available = [r.get("config") for r in results if isinstance(r, dict)]
                raise ValueError(
                    f"Config '{config_name}' not found in system run. Available configs: {available}"
                )
        else:
            # If no config_name specified, prefer intent_soft_noisy_0.2, else first completed
            for r in results:
                if isinstance(r, dict) and r.get("config") == "intent_soft_noisy_0.2":
                    target_res = r
                    break
            if not target_res and results:
                target_res = results[0]

        rows = target_res.get("rows", []) if target_res else []
    elif isinstance(data, dict) and "rows" in data and isinstance(data["rows"], list):
        rows = data["rows"]
    else:
        rows = []

    qid_to_top10: Dict[str, List[str]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        qid = str(r.get("qid", "")).strip()
        if not qid:
            continue
        top10 = (
            r.get("top10")
            or r.get("top10_ids")
            or r.get("top_k")
            or r.get("document_ids")
            or []
        )
        qid_to_top10[qid] = [str(x) for x in top10]

    return qid_to_top10


def evaluate_multilabel(
    system_top10: Dict[str, List[str]],
    labels_c: Dict[str, Dict[str, Any]],
    labels_a: Optional[Dict[str, Dict[str, Any]]] = None,
    labels_b: Optional[Dict[str, Dict[str, Any]]] = None,
    scope: str = "MAIN",
    split: Optional[str] = None,
    k_recall: int = 5,
    k_mrr: int = 10,
    k_ndcg: int = 5,
) -> Dict[str, Any]:
    """Execute multi-label evaluation across scope, bounds, and intent families.

    Enforces split discipline:
    1. Restricts scoring to qids matching --split ('test' -> 't', 'dev' -> 'd') or inferred.
    2. If any in-scope labeled qid of that split lacks a system row, raises an error.
    """
    # Determine split prefix
    split_prefix: Optional[str] = None
    if split:
        split_clean = split.lower().strip()
        if split_clean == "test":
            split_prefix = "t"
        elif split_clean == "dev":
            split_prefix = "d"
        else:
            raise ValueError(f"Invalid split '{split}'. Must be 'test' or 'dev'.")
    elif system_top10:
        # Infer split from system run qid prefixes
        if all(q.startswith("t") for q in system_top10.keys()):
            split_prefix = "t"
            split = "test"
        elif all(q.startswith("d") for q in system_top10.keys()):
            split_prefix = "d"
            split = "dev"

    # Restrict candidate labels to the matching split if specified/inferred
    if split_prefix:
        candidate_labels = {q: rec for q, rec in labels_c.items() if q.startswith(split_prefix)}
    else:
        candidate_labels = labels_c

    # Filter candidate labels by scope
    in_scope_labels, exclusions = filter_scope(candidate_labels, scope=scope)

    # Check for missing system rows among in-scope labeled queries
    missing_qids = [q for q in in_scope_labels if q not in system_top10]
    if missing_qids:
        raise ValueError(
            f"Evaluation error: {len(missing_qids)} in-scope labeled queries for split '{split or 'all'}' "
            f"are missing from the system run (e.g. {missing_qids[:5]}). System runs must cover all in-scope queries."
        )

    qids = sorted(list(in_scope_labels.keys()))
    n = len(qids)

    if n == 0:
        return {
            "scope": scope,
            "split": split,
            "exclusions": exclusions,
            "n_evaluated": 0,
            "consensus": {"recall@5": 0.0, "mrr@10": 0.0, "ndcg@5": 0.0},
            "lower": {"recall@5": 0.0, "mrr@10": 0.0, "ndcg@5": 0.0},
            "upper": {"recall@5": 0.0, "mrr@10": 0.0, "ndcg@5": 0.0},
            "by_intent": {},
            "decision_rule": {"verdict": "NOT_MET", "details": "No queries in scope"},
        }

    # Accumulators
    cons_recall_sum = 0.0
    cons_mrr_sum = 0.0
    cons_ndcg_sum = 0.0

    lower_recall_sum = 0.0
    lower_mrr_sum = 0.0
    lower_ndcg_sum = 0.0

    upper_recall_sum = 0.0
    upper_mrr_sum = 0.0
    upper_ndcg_sum = 0.0

    intent_groups: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "recall_sum": 0.0, "mrr_sum": 0.0, "ndcg_sum": 0.0}
    )

    for qid in qids:
        rec_c = in_scope_labels[qid]
        retrieved = system_top10[qid]

        # 1. Consensus relevant set (C)
        prim_c = rec_c.get("primary")
        rel_c = set(rec_c.get("relevant_set") or [])
        sec_c = [d for d in rel_c if d != prim_c]

        m_cons = compute_query_retrieval_metrics(
            retrieved, rel_c, prim_c, sec_c, k_recall, k_mrr, k_ndcg
        )
        cons_recall_sum += m_cons["recall"]
        cons_mrr_sum += m_cons["mrr"]
        cons_ndcg_sum += m_cons["ndcg"]

        # Per-intent grouping (via consensus primary)
        intent = infer_intent_from_doc_id(prim_c)
        intent_groups[intent]["n"] += 1
        intent_groups[intent]["recall_sum"] += m_cons["recall"]
        intent_groups[intent]["mrr_sum"] += m_cons["mrr"]
        intent_groups[intent]["ndcg_sum"] += m_cons["ndcg"]

        # 2. Lower bound: consensus primary document only ({primary})
        prim_low = prim_c
        rel_low = {prim_c} if prim_c else set()
        sec_low: List[str] = []

        m_low = compute_query_retrieval_metrics(
            retrieved, rel_low, prim_low, sec_low, k_recall, k_mrr, k_ndcg
        )
        lower_recall_sum += m_low["recall"]
        lower_mrr_sum += m_low["mrr"]
        lower_ndcg_sum += m_low["ndcg"]

        # 3. Upper bound: union of relevant documents (A ∪ B ∪ C)
        rec_a = labels_a.get(qid, {}) if labels_a else {}
        rec_b = labels_b.get(qid, {}) if labels_b else {}
        rel_a = set(rec_a.get("relevant_set") or [])
        rel_b = set(rec_b.get("relevant_set") or [])
        rel_up = (rel_a | rel_b | rel_c) - {None}
        prim_up = prim_c
        sec_up = [d for d in rel_up if d != prim_up]

        m_up = compute_query_retrieval_metrics(
            retrieved, rel_up, prim_up, sec_up, k_recall, k_mrr, k_ndcg
        )
        upper_recall_sum += m_up["recall"]
        upper_mrr_sum += m_up["mrr"]
        upper_ndcg_sum += m_up["ndcg"]

    consensus_summary = {
        "recall@5": round(cons_recall_sum / n, 4),
        "mrr@10": round(cons_mrr_sum / n, 4),
        "ndcg@5": round(cons_ndcg_sum / n, 4),
    }

    lower_summary = {
        "recall@5": round(lower_recall_sum / n, 4),
        "mrr@10": round(lower_mrr_sum / n, 4),
        "ndcg@5": round(lower_ndcg_sum / n, 4),
    }

    upper_summary = {
        "recall@5": round(upper_recall_sum / n, 4),
        "mrr@10": round(upper_mrr_sum / n, 4),
        "ndcg@5": round(upper_ndcg_sum / n, 4),
    }

    by_intent_summary = {}
    for intent, grp in sorted(intent_groups.items()):
        grp_n = grp["n"]
        by_intent_summary[intent] = {
            "n": grp_n,
            "recall@5": round(grp["recall_sum"] / grp_n, 4) if grp_n else 0.0,
            "mrr@10": round(grp["mrr_sum"] / grp_n, 4) if grp_n else 0.0,
            "ndcg@5": round(grp["ndcg_sum"] / grp_n, 4) if grp_n else 0.0,
        }

    # Evaluate decision rule (PLAN4 lines 42-46)
    target_r5 = 0.90
    target_mrr = 0.75
    target_ndcg = 0.80

    r5_met = consensus_summary["recall@5"] >= target_r5
    mrr_met = consensus_summary["mrr@10"] >= target_mrr
    ndcg_met = consensus_summary["ndcg@5"] >= target_ndcg
    all_met = r5_met and mrr_met and ndcg_met

    decision_rule = {
        "verdict": "MET" if all_met else "NOT_MET",
        "targets": {
            "recall@5": {"target": target_r5, "measured": consensus_summary["recall@5"], "met": r5_met},
            "mrr@10": {"target": target_mrr, "measured": consensus_summary["mrr@10"], "met": mrr_met},
            "ndcg@5": {"target": target_ndcg, "measured": consensus_summary["ndcg@5"], "met": ndcg_met},
        },
    }

    return {
        "scope": scope,
        "split": split,
        "n_evaluated": n,
        "exclusions": exclusions,
        "consensus": consensus_summary,
        "lower": lower_summary,
        "upper": upper_summary,
        "by_intent": by_intent_summary,
        "decision_rule": decision_rule,
    }


def generate_evaluation_report(
    eval_res: Dict[str, Any],
    agreement_res: Optional[Dict[str, Any]] = None,
    system_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Format full markdown evaluation report."""
    lines = [
        "# PLAN4 Multi-Relevant Retrieval Evaluation Report",
        "",
        f"- **Timestamp**: {datetime.now(timezone.utc).isoformat()}",
        f"- **Scope Evaluated**: `{eval_res.get('scope')}` (n={eval_res.get('n_evaluated')})",
        f"- **Split**: `{eval_res.get('split') or 'unspecified'}`",
    ]
    if system_info:
        for k, v in system_info.items():
            lines.append(f"- **{k}**: {v}")

    # 1. Annotator Agreement
    if agreement_res and agreement_res.get("n_common_queries", 0) > 0:
        lines.extend([
            "",
            "## 1. Inter-Annotator Agreement (Annotator A vs Annotator B)",
            "",
            f"Common Queries Annotated: **{agreement_res['n_common_queries']}**",
            "",
            "| Agreement Metric | Value | Description |",
            "| :--- | :---: | :--- |",
            f"| **Cohen's $\\kappa$ (support_request)** | {agreement_res['cohen_kappa_support_request']:.4f} | Binary agreement on whether message is a support request |",
            f"| **Exact-Match Rate (primary)** | {agreement_res['exact_match_primary']:.4f} | Fraction where both labelers selected identical primary doc |",
            f"| **Cohen's $\\kappa$ (primary)** | {agreement_res['cohen_kappa_primary']:.4f} | Multi-class categorical agreement across primary doc IDs |",
            f"| **Mean Jaccard Overlap** | {agreement_res['mean_jaccard_relevant']:.4f} | Average set overlap $|A \\cap B| / |A \\cup B|$ of relevant docs |",
            "",
            "> [!NOTE]",
            "> Annotators A and B are blind LLM evaluators per PLAN4 §Labels. Human validation sample remains pending.",
        ])

    # 2. Scope & Exclusion Accounting
    ex = eval_res.get("exclusions", {})
    lines.extend([
        "",
        "## 2. Scope & Exclusion Accounting",
        "",
        f"Total Queries Evaluated: **{ex.get('total_evaluated', 0)}** | In Scope (`{eval_res.get('scope')}`): **{ex.get('total_in_scope', 0)}** | Total Excluded: **{ex.get('total_excluded', 0)}**",
        "",
        "| Exclusion Reason | Count | Criteria |",
        "| :--- | :---: | :--- |",
        f"| `not_support_request` | {ex.get('not_support_request', 0)} | `support_request == False` (chatter, praise, out-of-domain) |",
        f"| `non_english` | {ex.get('non_english', 0)} | `language != 'en'` (multilingual queries out of v1 scope) |",
        f"| `no_primary` | {ex.get('no_primary', 0)} | `primary is null` (unanswerable from current FAQ catalog) |",
        f"| **Total Excluded** | **{ex.get('total_excluded', 0)}** | Sum of waterfall exclusions |",
    ])

    # 3. Retrieval Performance Table
    cons = eval_res.get("consensus", {})
    low = eval_res.get("lower", {})
    up = eval_res.get("upper", {})
    dec = eval_res.get("decision_rule", {})
    targets = dec.get("targets", {})

    r_target = targets.get("recall@5", {}).get("target", 0.90)
    mrr_target = targets.get("mrr@10", {}).get("target", 0.75)
    ndcg_target = targets.get("ndcg@5", {}).get("target", 0.80)

    lines.extend([
        "",
        "## 3. Retrieval Quality & Sensitivity Bounds",
        "",
        "| Relevant Set / Bound | recall@5 | MRR@10 | Graded nDCG@5 | TDD §6.4 Gate Status |",
        "| :--- | :---: | :---: | :---: | :---: |",
        f"| **LOWER Bound** (Consensus primary only) | {low.get('recall@5', 0):.4f} | {low.get('mrr@10', 0):.4f} | {low.get('ndcg@5', 0):.4f} | Sensitivity check |",
        f"| **CONSENSUS** (Adjudicator C) | **{cons.get('recall@5', 0):.4f}** | **{cons.get('mrr@10', 0):.4f}** | **{cons.get('ndcg@5', 0):.4f}** | **{dec.get('verdict', 'UNKNOWN')}** |",
        f"| **UPPER Bound** (Union A $\\cup$ B $\\cup$ C) | {up.get('recall@5', 0):.4f} | {up.get('mrr@10', 0):.4f} | {up.get('ndcg@5', 0):.4f} | Sensitivity check |",
        f"| *TDD §6.4 Target* | *≥ {r_target:.2f}* | *≥ {mrr_target:.2f}* | *≥ {ndcg_target:.2f}* | — |",
    ])

    # 4. Per-Intent Breakdown
    by_intent = eval_res.get("by_intent", {})
    lines.extend([
        "",
        "## 4. Consensus Retrieval by Intent Family",
        "",
        "| Intent Family | n | recall@5 | MRR@10 | Graded nDCG@5 |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ])
    for intent, stats in sorted(by_intent.items()):
        lines.append(
            f"| `{intent}` | {stats['n']} | {stats['recall@5']:.4f} | {stats['mrr@10']:.4f} | {stats['ndcg@5']:.4f} |"
        )

    # 5. Verdict
    verdict = dec.get("verdict", "NOT_MET")
    lines.extend([
        "",
        "## 5. PLAN4 Decision Rule Summary",
        "",
        f"- **Final Verdict**: **{verdict}**",
    ])
    for metric, tinfo in targets.items():
        status_sym = "PASSED" if tinfo.get("met") else "FAILED"
        lines.append(
            f"  - `{metric}`: measured **{tinfo.get('measured', 0):.4f}** vs target **{tinfo.get('target', 0):.2f}** [{status_sym}]"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="PLAN4 Multi-Relevant Retrieval Measurement & Evaluation Tooling"
    )
    parser.add_argument("--labels-a", type=str, default=None, help="Path to Annotator A labels JSONL")
    parser.add_argument("--labels-b", type=str, default=None, help="Path to Annotator B labels JSONL")
    parser.add_argument("--labels-c", type=str, required=True, help="Path to Adjudicator C labels JSONL")
    parser.add_argument(
        "--system-run",
        type=str,
        required=True,
        help="Path to JSON file containing system retrieval results or per-query rows",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Config name to select if system run JSON contains multiple configs",
    )
    parser.add_argument(
        "--split",
        type=str,
        default=None,
        choices=["test", "dev"],
        help="Evaluation split (test or dev) to enforce split discipline",
    )
    parser.add_argument(
        "--scope",
        type=str,
        default="MAIN",
        choices=["MAIN", "ALL-support"],
        help="Evaluation scope filter: MAIN or ALL-support",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Path to save evaluation output JSON",
    )
    args = parser.parse_args()

    # Load labels
    labels_a, labels_b, labels_c = load_labels(args.labels_a, args.labels_b, args.labels_c)
    if not labels_c:
        sys.stderr.write(f"Error: Adjudicator C labels ({args.labels_c}) empty or not found.\n")
        sys.exit(1)

    # Agreement stats
    agreement_stats = None
    if labels_a and labels_b:
        agreement_stats = compute_agreement_stats(labels_a, labels_b, split=args.split)

    # Load system rows
    system_top10 = load_system_rows(args.system_run, config_name=args.config)

    # Run evaluation
    eval_res = evaluate_multilabel(
        system_top10=system_top10,
        labels_c=labels_c,
        labels_a=labels_a,
        labels_b=labels_b,
        scope=args.scope,
        split=args.split,
    )

    system_info = {
        "System Run File": args.system_run,
        "Configuration": args.config or "(default)",
        "Split": eval_res.get("split") or "unspecified",
    }

    report_md = generate_evaluation_report(
        eval_res=eval_res,
        agreement_res=agreement_stats,
        system_info=system_info,
    )

    print("\n" + "=" * 80)
    print(report_md)
    print("=" * 80 + "\n")

    if args.out:
        out_p = pathlib.Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "split": eval_res.get("split"),
            "config": args.config,
            "system_run_file": args.system_run,
            "labels_a_file": args.labels_a,
            "labels_b_file": args.labels_b,
            "labels_c_file": args.labels_c,
            "agreement_stats": agreement_stats,
            "evaluation": eval_res,
            "markdown_report": report_md,
        }
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Saved evaluation report to: {out_p}")


if __name__ == "__main__":
    main()
