#!/usr/bin/env python3
"""import_threads.py — import the resolved-threads corpus into Agent Search (Discovery Engine).

Supports location parametrization (default: global, or us).
Reconciliation mode defaults to FULL.

Usage:
  python rag/import_threads.py --location us
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from ticket_router.common.auth import get_access_token
from ticket_router.retrieval.de_endpoint import api_base, documents_import_url

DEFAULT_BUCKET = "gs://susmith-fde-ticket-router-laya"
DEFAULT_URI = f"{DEFAULT_BUCKET}/corpus/resolved-threads/v1/resolved_threads_import.jsonl"
DEFAULT_PROJECT = "susmith-fde-ticket-router"
DEFAULT_DATA_STORE = "resolved-threads"


def get_token():
    return get_access_token()


def call(method, url, project, token, body=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8")[:600]
        raise RuntimeError(f"HTTP {e.code} on {method} {url}: {msg}")


def trigger_import(
    project=DEFAULT_PROJECT,
    location="global",
    data_store_id=DEFAULT_DATA_STORE,
    input_uri=DEFAULT_URI,
    reconciliation_mode="FULL",
    token=None,
):
    tok = token or get_token()
    url = documents_import_url(project, data_store_id, location=location)
    payload = {
        "gcsSource": {"inputUris": [input_uri], "dataSchema": "document"},
        "reconciliationMode": reconciliation_mode,
    }
    return call("POST", url, project, tok, body=payload)


def poll_operation(op_name, project=DEFAULT_PROJECT, location="global", token=None, poll_interval=15, max_attempts=240):
    tok = token or get_token()
    base = api_base(location)
    url = f"{base}{op_name}"
    for attempt in range(1, max_attempts + 1):
        op = call("GET", url, project, tok)
        if op.get("done"):
            return op
        time.sleep(poll_interval)
    raise TimeoutError(f"Operation {op_name} did not complete within {poll_interval * max_attempts} seconds")


def main():
    ap = argparse.ArgumentParser(description="Import resolved-threads into Discovery Engine dataStore.")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project ID")
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("--data-store", default=DEFAULT_DATA_STORE, help="Target dataStore ID")
    ap.add_argument("--uri", default=DEFAULT_URI, help="GCS input URI for JSONL document import")
    ap.add_argument("--reconciliation-mode", default="FULL", choices=["FULL", "INCREMENTAL"], help="Reconciliation mode")
    ap.add_argument("--poll-interval", type=int, default=15, help="Seconds between poll requests")
    a = ap.parse_args()

    token = get_token()
    print(f"Triggering import for dataStore='{a.data_store}' in location='{a.location}'...")
    op = trigger_import(
        project=a.project,
        location=a.location,
        data_store_id=a.data_store,
        input_uri=a.uri,
        reconciliation_mode=a.reconciliation_mode,
        token=token,
    )
    op_name = op.get("name")
    print(f"Import LRO started: {op_name}")

    final_op = poll_operation(
        op_name,
        project=a.project,
        location=a.location,
        token=token,
        poll_interval=a.poll_interval,
    )
    if final_op.get("error"):
        print(f"IMPORT FAILED: {json.dumps(final_op['error'])}", file=sys.stderr)
        sys.exit(1)

    resp = final_op.get("response", {})
    print(f"Import completed successfully: {json.dumps(resp, indent=2)}")
    metadata = final_op.get("metadata", {})
    if metadata:
        print(f"Operation metadata: {json.dumps(metadata, indent=2)}")


if __name__ == "__main__":
    main()
