#!/usr/bin/env python3
"""search_test.py — query the Agent Search app (support-kb-search) like the FaqResolverAgent will.

For each query: top-5 results from `faq-articles` (citable) with snippet, plus a grounded summary with
citations (LLM add-on), and top-3 similar `resolved-threads` (secondary evidence).

  python search_test.py --project P "what is the return window for opened electronics?" ...
"""
import argparse, json, pathlib, subprocess, sys, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from de_endpoint import search_url, data_store_name  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="susmith-fde-ticket-router"); ap.add_argument("--engine", default="support-kb-search")
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("queries", nargs="+")
    a = ap.parse_args()
    tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
    url = search_url(a.project, a.engine, location=a.location)
    faq_ds = data_store_name(a.project, "faq-articles", location=a.location)
    threads_ds = data_store_name(a.project, "resolved-threads", location=a.location)

    def post(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {tok}", "x-goog-user-project": a.project, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:800]}")

    for q in a.queries:
        print(f"\n=== {q}")
        faq = post({"query": q, "pageSize": 5, "relevanceThreshold": "LOWEST",
                    "dataStoreSpecs": [{"dataStore": faq_ds}],
                    "contentSearchSpec": {"snippetSpec": {"returnSnippet": True},
                                          "summarySpec": {"summaryResultCount": 3, "includeCitations": True, "ignoreAdversarialQuery": True,
                                                          "ignoreNonSummarySeekingQuery": False}}})
        for i, r in enumerate(faq.get("results", []), 1):
            d = r["document"]
            title = (d.get("structData") or {}).get("title") or (d.get("derivedStructData") or {}).get("title", "")
            print(f"  {i}. {d['id']}  [{(d.get('structData') or {}).get('intent_family', '')}]  {title[:90]}")
        s = faq.get("summary", {})
        print("  SUMMARY:", (s.get("summaryText") or "")[:600].replace("\n", " "))
        refs = (s.get("summaryWithMetadata") or {}).get("references", [])
        print("  CITATIONS:", [r.get("document", "").split("/")[-1] for r in refs])
        th = post({"query": q, "pageSize": 3, "dataStoreSpecs": [{"dataStore": threads_ds}]})
        for r in th.get("results", []):
            sd = r["document"].get("structData", {})
            print(f"     thread {sd.get('thread_id')} [{sd.get('intent')}] {sd.get('first_customer_message', '')[:100]}")


if __name__ == "__main__":
    main()
