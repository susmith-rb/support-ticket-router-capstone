#!/usr/bin/env python3
"""retrieval_eval.py — recall@k / MRR of Agent Search over `faq-articles` (silver-standard query set).

Queries: real customer first messages whose teacher topic was clustered into a theme that became an
article; the relevant doc is that theme's article. Threads used as writing examples for the article
(the first 15 with an agent reply, same rule as build_faq_corpus.py) are excluded to avoid leakage.
This is a silver set (teacher topic -> theme mapping), not human-judged; report it as such.

  python retrieval_eval.py --themes themes.json --threads threads.jsonl --import faq_articles_import.jsonl --n 300
"""
import argparse, json, random, re, subprocess, time, urllib.error, urllib.request


def clean(q):
    cleaned = re.sub(r"@\w+|\[(URL|ORDER|EMAIL|PHONE|CARD)\]", " ", q)
    return re.sub(r"\s+", " ", cleaned.replace("#", " ")).strip() or q


def compute_rank(target, ids):
    """Return 1-based rank of target in ids, or None."""
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


def search(q, url, tok, project, ds, clean_query=False, threshold="", expansion=False, sleep_sec=0.7):
    body = {"query": (clean(q) if clean_query else q)[:1000], "pageSize": 10, "dataStoreSpecs": [{"dataStore": ds}]}
    if threshold:
        body["relevanceThreshold"] = threshold
    if expansion:
        body["queryExpansionSpec"] = {"condition": "AUTO"}
        body["spellCorrectionSpec"] = {"mode": "AUTO"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {tok}", "x-goog-user-project": project, "Content-Type": "application/json"})
    err = "search failed"
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                time.sleep(sleep_sec)   # stay under the per-minute search quota of a new project
                return [x["document"]["id"] for x in json.loads(r.read()).get("results", [])]
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            if e.code == 429:
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            break
    raise SystemExit(err)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--themes", required=True); ap.add_argument("--threads", required=True); ap.add_argument("--import", dest="imp", required=True)
    ap.add_argument("--project", default="susmith-fde-ticket-router"); ap.add_argument("--engine", default="support-kb-search")
    ap.add_argument("--n", type=int, default=300); ap.add_argument("--k", type=int, default=5); ap.add_argument("--out", default="retrieval_eval.json")
    ap.add_argument("--threshold", default="", help="relevanceThreshold: LOWEST|LOW|MEDIUM|HIGH ('' = API default)")
    ap.add_argument("--expansion", action="store_true", help="queryExpansionSpec AUTO + spellCorrection AUTO")
    ap.add_argument("--clean", action="store_true", help="strip @mentions, [URL]/[ORDER] tokens, hashtags marks before searching")
    ap.add_argument("--sleep", type=float, default=0.7, help="seconds between queries (search quota)")
    ap.add_argument("--gold-map", default=None, help="path to gold map JSON (old_doc_or_theme_id -> new_doc_id)")
    a = ap.parse_args()

    gold_map = json.load(open(a.gold_map)) if a.gold_map else {}

    threads = {str(json.loads(l)["thread_id"]): json.loads(l) for l in open(a.threads)}
    live = {json.loads(l)["id"] for l in open(a.imp)}
    themes = json.load(open(a.themes))
    cands = []
    for th in themes:
        doc = re.sub(r"[^a-z0-9-]", "-", f"faq-{th['intent_family']}-{th['theme_id']}".lower().replace("_", "-"))[:120]
        if doc not in live:
            continue
        with_agent = [t for t in th["thread_ids"] if t in threads and any(x["role"] == "agent" for x in threads[t]["turns"])]
        examples = set(with_agent[:15])
        for t in th["thread_ids"]:
            if t in threads and t not in examples:
                cands.append((threads[t]["turns"][0]["text"], doc, th["intent_family"]))
    random.Random(13).shuffle(cands)
    qs = cands[: a.n]

    tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
    url = (f"https://discoveryengine.googleapis.com/v1/projects/{a.project}/locations/global/collections/default_collection/"
           f"engines/{a.engine}/servingConfigs/default_search:search")
    ds = f"projects/{a.project}/locations/global/collections/default_collection/dataStores/faq-articles"

    rows, rr = [], []
    for q, doc, intent in qs:
        target = gold_map.get(doc, doc) if gold_map else doc
        ids = search(q, url, tok, a.project, ds, clean_query=a.clean, threshold=a.threshold, expansion=a.expansion, sleep_sec=a.sleep)
        rank = compute_rank(target, ids)
        rows.append({"query": q, "relevant": target, "intent": intent, "rank": rank, "top3": ids[:3]})
        rr.append(1 / rank if rank else 0.0)

    n = len(rows)
    ranks = [r["rank"] for r in rows]
    by = {}
    for r in rows:
        by.setdefault(r["intent"], []).append(r)
    rep = {"config": {"threshold": a.threshold or "default", "expansion": a.expansion, "clean": a.clean},
           "n": n, "zero_result_queries": sum(1 for r in rows if not r["top3"]),
           "recall@1": round(compute_recall_at_k(ranks, 1, n), 3),
           "recall@3": round(compute_recall_at_k(ranks, 3, n), 3),
           "recall@5": round(compute_recall_at_k(ranks, 5, n), 3),
           "recall@10": round(compute_recall_at_k(ranks, 10, n), 3),
           "mrr@10": round(compute_mrr(ranks, n), 3),
           "by_intent": {k: {"n": len(v), "recall@5": round(compute_recall_at_k([r["rank"] for r in v], 5, len(v)), 3)} for k, v in sorted(by.items())},
           "misses_sample": [r for r in rows if not r["rank"] or r["rank"] > 5][:15]}
    json.dump({"report": rep, "rows": rows}, open(a.out, "w"), indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "misses_sample"}, indent=1))
    for m in rep["misses_sample"][:8]:
        print(f"  miss: rel={m['relevant']} got={m['top3'][:2]} q={m['query'][:110]}")


if __name__ == "__main__":
    main()
