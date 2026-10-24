#!/usr/bin/env python3
"""search_test.py — query the Agent Search app (support-kb-search) like the FaqResolverAgent will.

For each query: top-5 results from `faq-articles` (citable) with snippet, plus a grounded summary with
citations (LLM add-on), and top-3 similar `resolved-threads` (secondary evidence).

  python search_test.py --project P "what is the return window for opened electronics?" ...
  python search_test.py --filter 'status="active"' "where is my order"
  python search_test.py --intent order_status "where is my order"
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from de_endpoint import search_url, data_store_name  # noqa: E402
from search_client import SearchClient, clean_query, build_filter  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="susmith-fde-ticket-router")
    ap.add_argument("--engine", default="support-kb-search")
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("--filter", default="", help="custom filter expression or status filter (e.g. 'status=\"active\"')")
    ap.add_argument("--intent", default="", help="intent family to filter (e.g. 'order_status')")
    ap.add_argument("queries", nargs="+")
    a = ap.parse_args()

    tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
    url = search_url(a.project, a.engine, location=a.location)
    faq_ds = data_store_name(a.project, "faq-articles", location=a.location)
    threads_ds = data_store_name(a.project, "resolved-threads", location=a.location)

    # Normalize filter syntax if user passed e.g. status="active"
    raw_filter = a.filter
    if raw_filter and 'status="' in raw_filter and 'status:' not in raw_filter:
        m = re.search(r'status="([^"]+)"', raw_filter)
        if m:
            raw_filter = f'status: ANY("{m.group(1)}")'

    filter_expr = build_filter(
        intent_family=a.intent or None,
        custom_filter=raw_filter or None,
    )

    def post(target_url, body):
        req = urllib.request.Request(
            target_url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {tok}",
                "x-goog-user-project": a.project,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:800]}")

    for q in a.queries:
        print(f"\n=== {q}")
        faq_body = {
            "query": q,
            "pageSize": 5,
            "relevanceThreshold": "LOWEST",
            "dataStoreSpecs": [{"dataStore": faq_ds}],
            "contentSearchSpec": {
                "snippetSpec": {"returnSnippet": True},
                "summarySpec": {
                    "summaryResultCount": 3,
                    "includeCitations": True,
                    "ignoreAdversarialQuery": True,
                    "ignoreNonSummarySeekingQuery": False,
                },
            },
        }
        if filter_expr:
            faq_body["filter"] = filter_expr

        faq = post(url, faq_body)

        # Fallback check if intent was used and fewer than 2 results
        if a.intent and len(faq.get("results", [])) < 2:
            print(f"  [Intent filter '{a.intent}' returned {len(faq.get('results', []))} results; falling back to unfiltered search]")
            fallback_body = dict(faq_body)
            if raw_filter:
                fallback_body["filter"] = raw_filter
            else:
                fallback_body.pop("filter", None)
            faq = post(url, fallback_body)

        for i, r in enumerate(faq.get("results", []), 1):
            d = r.get("document", {})
            struct_data = d.get("structData") or d.get("derivedStructData") or {}
            title = struct_data.get("title", "")
            intent = struct_data.get("intent_family", "")
            print(f"  {i}. {d.get('id', '')}  [{intent}]  {title[:90]}")

        s = faq.get("summary", {})
        print("  SUMMARY:", (s.get("summaryText") or "")[:600].replace("\n", " "))
        refs = (s.get("summaryWithMetadata") or {}).get("references", [])
        print("  CITATIONS:", [r.get("document", "").split("/")[-1] for r in refs])

        th = post(url, {"query": q, "pageSize": 3, "dataStoreSpecs": [{"dataStore": threads_ds}]})
        for r in th.get("results", []):
            sd = r.get("document", {}).get("structData", {})
            print(f"     thread {sd.get('thread_id')} [{sd.get('intent')}] {sd.get('first_customer_message', '')[:100]}")


if __name__ == "__main__":
    main()
