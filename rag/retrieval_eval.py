#!/usr/bin/env python3
"""retrieval_eval.py — recall@k / MRR / nDCG of Agent Search over `faq-articles`.

Queries:
- If --queries <file> is provided (JSONL or JSON), evaluates the given query set.
- Otherwise, samples from --themes, --threads, --import.

Supports:
- Location selection (--location global|us)
- Status filtering (--filter-status active,draft|active|off) (REQ-RAG-RETR-009)
- Intent family filtering with fallback (--intent-filter off|oracle|noisy) (REQ-RAG-RETR-010)
- Detailed latency percentiles and nDCG metrics via rag/eval_metrics.py (TDD §6.4)
"""

import argparse
import json
import math
import pathlib
import random
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from de_endpoint import search_url, data_store_name  # noqa: E402
from search_client import (  # noqa: E402
    SearchClient,
    SearchResult,
    clean_query as client_clean_query,
    VALID_INTENT_FAMILIES,
    DEFAULT_ALLOWED_STATUSES,
)

try:
    import eval_metrics
except ImportError:
    eval_metrics = None


def clean(q: str) -> str:
    """Backward-compatible query cleaner."""
    return client_clean_query(q)


def compute_rank(target, ids):
    """Return 1-based rank of target in ids, or None."""
    if eval_metrics:
        return eval_metrics.compute_rank(target, ids)
    if isinstance(target, (list, set, tuple)):
        ranks = [ids.index(t) + 1 for t in target if t in ids]
        return min(ranks) if ranks else None
    return ids.index(target) + 1 if target in ids else None


def compute_recall_at_k(ranks, k, n=None):
    total = n if n is not None else len(ranks)
    if total == 0:
        return 0.0
    return sum(1 for r in ranks if r is not None and r <= k) / total


def compute_mrr(ranks, n=None):
    total = n if n is not None else len(ranks)
    if total == 0:
        return 0.0
    return sum(1.0 / r for r in ranks if r) / total


def compute_ndcg_at_k(ranks, k=5, n=None):
    if eval_metrics:
        return eval_metrics.ndcg_at_k(ranks, k, n)
    total = n if n is not None else len(ranks)
    if total == 0:
        return 0.0
    dcg = sum(1.0 / math.log2(r + 1) for r in ranks if r is not None and r <= k)
    return dcg / total


def search(
    q,
    url,
    tok,
    project,
    ds,
    clean_query=False,
    threshold="",
    expansion=False,
    sleep_sec=0.7,
    allowed_statuses=None,
    intent_family=None,
    fallback_min_results=2,
    ranking_expression="",
    client=None,
    **kwargs,
):
    """Executes search via SearchClient or legacy direct request."""
    if client is not None:
        return client.search(
            query=q,
            page_size=10,
            threshold=threshold,
            clean=clean_query,
            allowed_statuses=allowed_statuses,
            intent_family=intent_family,
            fallback_min_results=fallback_min_results,
            ranking_expression=ranking_expression,
            query_expansion=expansion,
            sleep_sec=sleep_sec,
            token=tok,
        )

    # Standalone execution if no client passed
    body: Dict[str, Any] = {
        "query": (clean(q) if clean_query else q)[:1000],
        "pageSize": 10,
        "dataStoreSpecs": [{"dataStore": ds}],
    }
    if threshold:
        body["relevanceThreshold"] = threshold
    if expansion:
        body["queryExpansionSpec"] = {"condition": "AUTO"}
        body["spellCorrectionSpec"] = {"mode": "AUTO"}
    if ranking_expression:
        body["rankingExpression"] = ranking_expression

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {tok}",
            "x-goog-user-project": project,
            "Content-Type": "application/json",
        },
    )
    err = "search failed"
    for attempt in range(8):
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            lat = (time.perf_counter() - t0) * 1000.0
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            doc_ids = [x["document"]["id"] for x in data.get("results", [])]
            return SearchResult(
                document_ids=doc_ids,
                latency_ms=round(lat, 2),
                total_size=data.get("totalSize", len(doc_ids)),
                filter_applied="",
                fallback_triggered=False,
                raw_results=data.get("results", []),
            )
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            if e.code == 429:
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            break
    raise SystemExit(err)


def load_queries_from_file(queries_path: pathlib.Path) -> List[Dict[str, Any]]:
    """Loads a query set from JSONL or JSON."""
    if queries_path.suffix == ".jsonl":
        raw_items = [json.loads(line) for line in queries_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        data = json.loads(queries_path.read_text(encoding="utf-8"))
        raw_items = data.get("rows", data if isinstance(data, list) else [])

    qs = []
    for idx, r in enumerate(raw_items):
        q_text = r.get("query") or r.get("text", "")
        rel = r.get("relevant") or r.get("doc") or r.get("original_gold", "")
        intent = r.get("intent") or r.get("intent_family", "unknown")
        orig_gold = r.get("original_gold") or rel
        qid = r.get("qid") or f"q{idx:03d}"
        qs.append({
            "qid": qid,
            "query": q_text,
            "relevant": rel,
            "original_gold": orig_gold,
            "intent": intent,
        })
    return qs


def load_queries_from_themes(themes_path, threads_path, import_path) -> List[Dict[str, Any]]:
    """Legacy query sampler from themes, threads, and import files."""
    threads = {str(json.loads(l)["thread_id"]): json.loads(l) for l in open(threads_path)}
    live = {json.loads(l)["id"] for l in open(import_path)}
    themes = json.load(open(themes_path))
    cands = []
    for th in themes:
        doc = re.sub(r"[^a-z0-9-]", "-", f"faq-{th['intent_family']}-{th['theme_id']}".lower().replace("_", "-"))[:120]
        if doc not in live:
            continue
        with_agent = [t for t in th["thread_ids"] if t in threads and any(x["role"] == "agent" for x in threads[t]["turns"])]
        examples = set(with_agent[:15])
        for t in th["thread_ids"]:
            if t in threads and t not in examples:
                cands.append({
                    "qid": f"th_{t}",
                    "query": threads[t]["turns"][0]["text"],
                    "relevant": doc,
                    "original_gold": doc,
                    "intent": th["intent_family"],
                })
    random.Random(13).shuffle(cands)
    return cands


def main():
    ap = argparse.ArgumentParser(description="Evaluate Agent Search retrieval quality.")
    ap.add_argument("--queries", default=None, help="path to queries JSONL or JSON file")
    ap.add_argument("--themes", default=None)
    ap.add_argument("--threads", default=None)
    ap.add_argument("--import", dest="imp", default=None)
    ap.add_argument("--project", default="susmith-fde-ticket-router")
    ap.add_argument("--engine", default="support-kb-search")
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default="retrieval_eval.json")
    ap.add_argument("--threshold", default="", help="relevanceThreshold: LOWEST|LOW|MEDIUM|HIGH ('' = API default)")
    ap.add_argument("--expansion", action="store_true", help="queryExpansionSpec AUTO + spellCorrection AUTO")
    ap.add_argument("--clean", action="store_true", help="strip @mentions, [URL]/[ORDER] tokens, hashtags before searching")
    ap.add_argument("--sleep", type=float, default=0.7, help="seconds between queries (search quota)")
    ap.add_argument("--gold-map", default=None, help="path to gold map JSON (old_doc_or_theme_id -> new_doc_id)")
    ap.add_argument("--filter-status", default="active,draft", help="allowed statuses (e.g. 'active,draft', 'active', or 'off')")
    ap.add_argument("--intent-filter", choices=["off", "oracle", "noisy"], default="off", help="intent filtering mode")
    ap.add_argument("--ranking-expression", default="", help="custom rankingExpression (e.g. semantic/keyword blend)")
    ap.add_argument("--fallback-min-results", type=int, default=2, help="min filtered results before falling back")
    a = ap.parse_args()

    gold_map = json.load(open(a.gold_map)) if a.gold_map else {}

    if a.queries:
        qs = load_queries_from_file(pathlib.Path(a.queries))
    elif a.themes and a.threads and a.imp:
        qs = load_queries_from_themes(a.themes, a.threads, a.imp)
    else:
        raise SystemExit("Must provide either --queries <file> OR (--themes, --threads, --import)")

    if a.n:
        qs = qs[: a.n]

    # Parse allowed statuses
    allowed_statuses = None
    if a.filter_status and a.filter_status.lower() not in ("off", "none", "all", ""):
        allowed_statuses = [s.strip() for s in a.filter_status.split(",") if s.strip()]

    tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
    url = search_url(a.project, a.engine, location=a.location)
    ds = data_store_name(a.project, "faq-articles", location=a.location)

    client = SearchClient(
        project=a.project,
        engine=a.engine,
        data_store="faq-articles",
        location=a.location,
        token=tok,
        default_allowed_statuses=allowed_statuses,
        sleep_sec=a.sleep,
    )

    rows, rr = [], []
    noisy_rng = random.Random(42)

    for idx, item in enumerate(qs):
        q = item["query"]
        doc = item["relevant"]
        intent = item["intent"]
        target = gold_map.get(doc, doc) if gold_map else doc

        # Intent filtering logic
        intent_family = None
        applied_intent = None
        if a.intent_filter == "oracle":
            intent_family = intent
            applied_intent = intent
        elif a.intent_filter == "noisy":
            if noisy_rng.random() < 0.14:  # 14% router error rate
                alt = [f for f in VALID_INTENT_FAMILIES if f != intent]
                intent_family = noisy_rng.choice(alt) if alt else intent
            else:
                intent_family = intent
            applied_intent = intent_family

        res = search(
            q=q,
            url=url,
            tok=tok,
            project=a.project,
            ds=ds,
            clean_query=a.clean,
            threshold=a.threshold,
            expansion=a.expansion,
            sleep_sec=a.sleep,
            allowed_statuses=allowed_statuses,
            intent_family=intent_family,
            fallback_min_results=a.fallback_min_results,
            ranking_expression=a.ranking_expression,
            client=client,
        )

        if isinstance(res, SearchResult):
            retrieved_ids = res.document_ids
            lat_ms = res.latency_ms
            fb_triggered = res.fallback_triggered
            filter_applied = res.filter_applied
        else:
            retrieved_ids = list(res)
            lat_ms = 0.0
            fb_triggered = False
            filter_applied = ""

        rank = compute_rank(target, retrieved_ids)
        rows.append({
            "qid": item.get("qid", f"q{idx:03d}"),
            "query": q,
            "relevant": target,
            "original_gold": item.get("original_gold", doc),
            "intent": intent,
            "applied_intent": applied_intent,
            "rank": rank,
            "top3": retrieved_ids[:3],
            "top_k": retrieved_ids,
            "latency_ms": lat_ms,
            "fallback_triggered": fb_triggered,
            "filter_applied": filter_applied,
        })
        rr.append(1.0 / rank if rank else 0.0)

    # Compute overall metrics
    if eval_metrics:
        summary_all = eval_metrics.compute_metrics_summary(rows)
    else:
        n = len(rows)
        ranks = [r["rank"] for r in rows]
        by = {}
        for r in rows:
            by.setdefault(r["intent"], []).append(r)
        summary_all = {
            "n": n,
            "zero_result_queries": sum(1 for r in rows if not r["top3"]),
            "recall@1": round(compute_recall_at_k(ranks, 1, n), 4),
            "recall@3": round(compute_recall_at_k(ranks, 3, n), 4),
            "recall@5": round(compute_recall_at_k(ranks, 5, n), 4),
            "recall@10": round(compute_recall_at_k(ranks, 10, n), 4),
            "mrr@10": round(compute_mrr(ranks, n), 4),
            "ndcg@5": round(compute_ndcg_at_k(ranks, 5, n), 4),
            "by_intent": {k: {"n": len(v), "recall@5": round(compute_recall_at_k([r["rank"] for r in v], 5, len(v)), 4)} for k, v in sorted(by.items())},
        }

    # Compute slice (b) metrics if gold_map has dropped set
    slice_b_summary = None
    if eval_metrics and gold_map and "dropped" in gold_map:
        rows_b = eval_metrics.filter_slice(rows, gold_map, "b")
        slice_b_summary = eval_metrics.compute_metrics_summary(rows_b)

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    latency_summary = (
        eval_metrics.latency_percentiles(latencies)
        if eval_metrics
        else {"count": len(latencies), "p50": None, "p95": None}
    )

    rep = {
        "config": {
            "threshold": a.threshold or "default",
            "expansion": a.expansion,
            "clean": a.clean,
            "filter_status": a.filter_status,
            "intent_filter": a.intent_filter,
            "ranking_expression": a.ranking_expression,
            "location": a.location,
        },
        "n": len(rows),
        "zero_result_queries": summary_all["zero_result_queries"],
        "recall@1": summary_all["recall@1"],
        "recall@3": summary_all["recall@3"],
        "recall@5": summary_all["recall@5"],
        "recall@10": summary_all["recall@10"],
        "mrr@10": summary_all["mrr@10"],
        "ndcg@5": summary_all["ndcg@5"],
        "latency_ms": latency_summary,
        "fallback_count": sum(1 for r in rows if r.get("fallback_triggered")),
        "by_intent": summary_all["by_intent"],
        "slice_b": slice_b_summary,
        "misses_sample": [r for r in rows if not r["rank"] or r["rank"] > 5][:15],
    }

    out_path = pathlib.Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"report": rep, "rows": rows}, open(out_path, "w"), indent=1)

    print(json.dumps({k: v for k, v in rep.items() if k != "misses_sample"}, indent=1))
    for m in rep["misses_sample"][:8]:
        print(f"  miss: rel={m['relevant']} got={m['top3'][:2]} q={m['query'][:110]}")


if __name__ == "__main__":
    main()
