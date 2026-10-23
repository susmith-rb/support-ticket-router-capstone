#!/usr/bin/env python3
"""publish_faq.py — versioned, idempotent publish of the `faq-articles` corpus (TDD §6.6.3).

  python publish_faq.py --work out_dir --project P [--release N] [--dry-run] [--lint-only]
  python publish_faq.py --work out_dir --retire faq-other-foo [--supersedes faq-other-bar]
  python publish_faq.py --work out_dir --activate faq-returns-window --reviewed-by susmith

Steps (each idempotent):
  1. lint      numeric policy facts in article bodies must appear in SIMULATED_POLICY (single-sourced policy)
  2. stamp     diff docs/ against the previous manifest; bump `version` where content_sha256 changed,
               keep it where not; carry status/effective_*/supersedes/review fields forward
  3. upload    docs + manifest to gs://…/corpus/faq-articles/v<N>/ (previous releases stay for rollback)
  4. import    documents:import with reconciliationMode=FULL (manifest = source of truth; retired or
               removed articles disappear from the index without a separate purge)
  5. wait      poll the import LRO
  6. verify    search for a changed article's title; require the served `version` to match
  7. report    changed/added/retired ids → feed cache eviction (FAQ_VERSION bump) and the frozen-set
               retrieval regression (retrieval_eval.py; gate Δrecall@5 ≥ −0.02)

Rollback = `--release <previous N>` with the previous manifest, nothing else.
"""
import argparse, datetime, hashlib, json, pathlib, re, subprocess, sys, time, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from build_faq_corpus import POLICY_VERSION, SIMULATED_POLICY  # noqa: E402
from de_endpoint import api_base, data_store_name, documents_import_url, search_url  # noqa: E402

BUCKET = "gs://susmith-fde-ticket-router-laya"


def tok():
    return subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()


def call(method, url, project, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": f"Bearer {tok()}", "x-goog-user-project": project,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> {e.code} {e.read().decode()[:400]}")


def sha(text): return hashlib.sha256(text.encode()).hexdigest()


# ---------- 1. policy lint ----------
NUM = re.compile(r"\$\s*(\d+)|\b(\d+)\s*(business day|day|hour|h\b|%|\$)", re.I)
RANGE = re.compile(r"\b(\d+)\s*[-–]\s*(\d+)\s*(business day|day|hour|h\b|%|\$)", re.I)


def answer_only(body):
    """Drop the 'Also asked as:' paraphrase block — customer phrasing ("why is 2-day taking 3 days?") is not policy."""
    out, skip = [], False
    for l in body.splitlines():
        if l.strip() == "Also asked as:": skip = True; continue
        if skip and not l.strip(): skip = False
        if not skip: out.append(l)
    return "\n".join(out)


def lint(docs):
    allowed = set()
    for m in NUM.finditer(SIMULATED_POLICY):
        if m.group(1):
            allowed.add(m.group(1))
        elif m.group(2):
            allowed.add(m.group(2))
    allowed |= set(re.findall(r"\$\s*(\d+)", SIMULATED_POLICY))
    for m in RANGE.finditer(SIMULATED_POLICY):
        allowed.add(m.group(1))
        allowed.add(m.group(2))

    bad = {}
    for doc_id, body in docs.items():
        hits = set()
        for m in NUM.finditer(answer_only(body)):
            if m.group(1):
                num = m.group(1)
                hit_str = f"${num}"
            else:
                num = m.group(2)
                hit_str = f"{num} {m.group(3)}"
            if num not in allowed:
                hits.add(hit_str)
        if hits:
            bad[doc_id] = sorted(hits)
    return bad


# ---------- 2. stamp ----------
def load_manifest(path):
    return {json.loads(l)["id"]: json.loads(l) for l in open(path)} if path.exists() else {}


def build_import_payload(manifest):
    """Return document entries for documents:import, excluding retired tombstones so reconciliationMode=FULL purges them."""
    return [m for m in manifest if m.get("structData", {}).get("status") != "retired"]


def stamp(docs, prev, gcs_prefix, today):
    out, changes = [], {"added": [], "changed": [], "unchanged": [], "retired": []}
    for doc_id, body in sorted(docs.items()):
        h = sha(body); p = prev.get(doc_id)
        if p is None:
            sd = {"article_id": doc_id, "title": body.splitlines()[0].lstrip("# ").strip(), "version": 1, "status": "draft",
                  "effective_from": today, "effective_to": "", "supersedes": "", "policy_version": POLICY_VERSION,
                  "reviewed_by": "", "reviewed_at": "", "simulated_policy": True}
            changes["added"].append(doc_id)
        else:
            sd = dict(p["structData"]); sd.setdefault("article_id", doc_id); sd.setdefault("status", "draft")
            if sd.get("content_sha256") is None:
                changes["unchanged"].append(doc_id)          # first adoption of a pre-lifecycle manifest: baseline, no bump
            elif sd.get("content_sha256") != h:
                sd["version"] = int(sd.get("version", 1)) + 1; sd["policy_version"] = POLICY_VERSION
                sd["reviewed_by"], sd["reviewed_at"] = "", ""      # an edit needs a fresh review
                if sd["status"] == "active": sd["status"] = "draft"
                changes["changed"].append(f"{doc_id}@{sd['version']}")
            else:
                changes["unchanged"].append(doc_id)
        sd["content_sha256"] = h
        out.append({"id": doc_id, "structData": sd, "content": {"mimeType": "text/plain", "uri": f"{gcs_prefix}/docs/{doc_id}.txt"}})
    for doc_id, p in prev.items():                       # kept in the manifest as retired tombstones
        if doc_id not in docs:
            sd = dict(p["structData"]); sd["status"] = "retired"; sd["effective_to"] = sd.get("effective_to") or today
            out.append({"id": doc_id, "structData": sd, "content": p["content"]}); changes["retired"].append(doc_id)
    return out, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="dir with docs/*.txt and manifest.jsonl")
    ap.add_argument("--project", default="susmith-fde-ticket-router")
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("--release", type=int, help="release number N for gs://…/v<N>/ (default: previous+1)")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--lint-only", action="store_true")
    ap.add_argument("--retire", nargs="*", default=[]); ap.add_argument("--supersedes", default="")
    ap.add_argument("--activate", nargs="*", default=[]); ap.add_argument("--reviewed-by", default="")
    ap.add_argument("--allow-lint", action="store_true", help="publish even with lint findings")
    a = ap.parse_args()
    w = pathlib.Path(a.work); today = datetime.date.today().isoformat()
    manifest_path = w / "manifest.jsonl"
    if not manifest_path.exists() and (w / "faq_articles_import.jsonl").exists():
        manifest_path = w / "faq_articles_import.jsonl"     # first run: adopt the v1 import file
    prev = load_manifest(manifest_path)
    rel = a.release or (int(json.load(open(w / "release.json"))["release"]) + 1 if (w / "release.json").exists() else 2)
    gcs_prefix = f"{BUCKET}/corpus/faq-articles/v{rel}"

    docs = {p.stem: p.read_text(encoding="utf-8") for p in sorted((w / "docs").glob("*.txt"))}
    for doc_id in a.retire:                                # retire = remove from docs/, keep tombstone
        (w / "dropped").mkdir(exist_ok=True)
        if (w / "docs" / f"{doc_id}.txt").exists(): (w / "docs" / f"{doc_id}.txt").rename(w / "dropped" / f"{doc_id}.txt")
        docs.pop(doc_id, None)

    bad = lint(docs)
    if bad:
        print("POLICY LINT: numbers not in SIMULATED_POLICY (policy_version %s):" % POLICY_VERSION)
        for k, v in bad.items(): print(f"  {k}: {', '.join(v)}")
    else:
        print("policy lint clean")
    if a.lint_only or (bad and not a.allow_lint and not a.dry_run):
        sys.exit(1 if bad else 0)

    manifest, changes = stamp(docs, prev, gcs_prefix, today)
    for m in manifest:
        sd = m["structData"]
        if sd["article_id"] in a.activate:
            sd["status"], sd["reviewed_by"], sd["reviewed_at"] = "active", a.reviewed_by or "unknown", today
        if a.supersedes and sd["article_id"] in a.retire: pass
        if a.supersedes and sd["article_id"] == a.supersedes: sd["supersedes"] = ",".join(a.retire)
    n_active = sum(1 for m in manifest if m["structData"]["status"] == "active")
    print(json.dumps({"release": rel, **{k: len(v) for k, v in changes.items()}, "active": n_active, "total": len(manifest)}))
    for k in ("added", "changed", "retired"):
        if changes[k]: print(f"  {k}: {changes[k]}")
    if a.dry_run: return

    with open(w / "manifest.jsonl", "w") as fh:
        for m in manifest: fh.write(json.dumps(m) + "\n")
    import_payload = build_import_payload(manifest)
    with open(w / "import.jsonl", "w") as fh:
        for m in import_payload: fh.write(json.dumps(m) + "\n")
    json.dump({"release": rel, "published_at": today, "policy_version": POLICY_VERSION, "changes": changes}, open(w / "release.json", "w"), indent=1)

    # 3. upload (docs for every manifest entry incl. tombstones, so FULL import can still fetch content)
    subprocess.run(["gcloud", "storage", "cp", "-r", str(w / "docs"), f"{gcs_prefix}/", "--project", a.project], check=True, capture_output=True)
    for doc_id in changes["retired"]:
        src = w / "dropped" / f"{doc_id}.txt"
        if src.exists(): subprocess.run(["gcloud", "storage", "cp", str(src), f"{gcs_prefix}/docs/", "--project", a.project], check=True, capture_output=True)
    subprocess.run(["gcloud", "storage", "cp", str(w / "manifest.jsonl"), f"{gcs_prefix}/manifest.jsonl", "--project", a.project], check=True, capture_output=True)
    subprocess.run(["gcloud", "storage", "cp", str(w / "import.jsonl"), f"{gcs_prefix}/import.jsonl", "--project", a.project], check=True, capture_output=True)
    print(f"uploaded -> {gcs_prefix}")

    # 4-5. FULL import + wait
    api = api_base(a.location)
    ds = data_store_name(a.project, "faq-articles", location=a.location)
    import_url = documents_import_url(a.project, "faq-articles", location=a.location)
    op = call("POST", import_url, a.project,
              {"gcsSource": {"inputUris": [f"{gcs_prefix}/import.jsonl"], "dataSchema": "document"}, "reconciliationMode": "FULL"})
    name = op["name"]; print("import op", name)
    for _ in range(120):
        op = call("GET", f"{api}{name}", a.project)
        if op.get("done"): break
        time.sleep(10)
    if not op.get("done") or op.get("error"):
        sys.exit(f"import failed/timeout: {json.dumps(op)[:600]}")
    print("import done:", json.dumps(op.get("response", {}))[:300])

    # 6. verify a changed (or added) article is served at its new version before anyone flips FAQ_VERSION
    probe = (changes["changed"] or changes["added"] or changes["unchanged"] or [None])[0]
    search_endpoint = search_url(a.project, "support-kb-search", location=a.location)
    if probe:
        pid = probe.split("@")[0]; want = next(m["structData"]["version"] for m in manifest if m["id"] == pid)
        title = next(m["structData"]["title"] for m in manifest if m["id"] == pid)
        for i in range(30):
            res = call("POST", search_endpoint, a.project,
                       {"query": title, "pageSize": 5, "relevanceThreshold": "LOWEST",
                        "dataStoreSpecs": [{"dataStore": ds}]})
            got = {r["document"]["id"]: r["document"].get("structData", {}).get("version") for r in res.get("results", [])}
            if got.get(pid) == want:
                print(f"verified: {pid} served at version {want} (attempt {i+1})"); break
            time.sleep(20)
        else:
            sys.exit(f"NOT VERIFIED: {pid} expected v{want}, saw {got} — do not bump FAQ_VERSION yet")

    # verify retired articles are NOT returned
    if changes["retired"]:
        ret_id = changes["retired"][0]
        ret_title = prev.get(ret_id, {}).get("structData", {}).get("title") or next(
            (m["structData"]["title"] for m in manifest if m["id"] == ret_id), ret_id
        )
        for i in range(30):
            res = call("POST", search_endpoint, a.project,
                       {"query": ret_title, "pageSize": 10, "relevanceThreshold": "LOWEST",
                        "dataStoreSpecs": [{"dataStore": ds}]})
            ret_ids = [r["document"]["id"] for r in res.get("results", [])]
            if ret_id not in ret_ids:
                print(f"verified: retired doc '{ret_id}' is absent from search results for '{ret_title}' (attempt {i+1})")
                break
            time.sleep(20)
        else:
            sys.exit(f"NOT VERIFIED: retired doc '{ret_id}' still present in search results: {ret_ids}")

    # 7. hand-off for cache eviction + regression gate
    evict = [c.split("@")[0] for c in changes["changed"]] + changes["retired"]
    print("next: FAQ_VERSION=%d; evict cache for article_ids %s; run retrieval_eval.py and gate Δrecall@5 ≥ -0.02" % (rel, evict))


if __name__ == "__main__":
    main()
