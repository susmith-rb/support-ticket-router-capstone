#!/usr/bin/env python3
"""clean_eval_breakdown.py — abstention vs retrieval rank, unsupported claims, and an Eval Service retry with an explicit autorater."""
import json, sys, subprocess, urllib.request, urllib.error


def esc(x):
    """Return True if answer was escalated or not grounded."""
    gen = x.get("gen", {})
    return bool(gen.get("escalate")) or not gen.get("grounded", True)


def compute_breakdown(ans, jd=None):
    """Compute summary breakdown of answers: abstention vs retrieval rank, and citations."""
    hit = [x for x in ans if x.get("rank") and x["rank"] <= 5]
    miss = [x for x in ans if not x.get("rank") or x["rank"] > 5]
    abstained_hit = sum(map(esc, hit))
    abstained_miss = sum(map(esc, miss))
    answered = [x for x in ans if not esc(x)]
    cited_source = sum(1 for x in answered if any(c.get("doc_id") == x.get("source_doc") for c in x.get("gen", {}).get("citations", [])))
    unsupported = []
    if jd:
        for j in jd:
            for c in j.get("claims", []):
                if not c.get("supported"):
                    unsupported.append(c.get("claim", ""))
    return {
        "total": len(ans),
        "hit_count": len(hit),
        "miss_count": len(miss),
        "abstained_hit": abstained_hit,
        "abstained_miss": abstained_miss,
        "abstained_hit_rate": (abstained_hit / len(hit)) if hit else 0.0,
        "abstained_miss_rate": (abstained_miss / len(miss)) if miss else 0.0,
        "answered_count": len(answered),
        "answered_cites_source": cited_source,
        "answered_cites_source_rate": (cited_source / len(answered)) if answered else 0.0,
        "unsupported_claims": unsupported,
    }


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python clean_eval_breakdown.py clean_eval_dir [docs_dir]")
        return 0

    W = args[0]
    ans = [json.loads(l) for l in open(f"{W}/answers.jsonl")]
    jd = [json.loads(l) for l in open(f"{W}/judge.jsonl")] if (pathlib_path := urllib.request.os.path.exists(f"{W}/judge.jsonl")) else []

    bd = compute_breakdown(ans, jd)
    print(f"abstained when source article in top-5: {bd['abstained_hit']}/{bd['hit_count']}; when NOT in top-5: {bd['abstained_miss']}/{bd['miss_count']}")
    print(f"answered (not escalated): {bd['answered_count']}; of those, cite the source article: {bd['answered_cites_source']}")

    hit = [x for x in ans if x.get("rank") and x["rank"] <= 5]
    print("\nsample abstentions where source WAS retrieved:")
    for x in [x for x in hit if esc(x)][:4]:
        print("  -", x.get("question", "")[:120], "| rank", x.get("rank"), "|", x.get("gen", {}).get("answer", "")[:120])

    if bd["unsupported_claims"]:
        print("\nunsupported claims (judge):")
        for c in bd["unsupported_claims"][:8]:
            print("  -", c[:150])

    if len(args) > 1:
        # Eval Service retry with explicit autorater on 3 answers
        docs_dir = args[1]
        P = "susmith-fde-ticket-router"
        tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
        for model in ("gemini-3.8-flash", "gemini-3.1-pro-preview"):
            for loc in ("global", "us-central1"):
                host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
                url = f"https://{host}/v1/projects/{P}/locations/{loc}:evaluateInstances"
                x = ans[0]
                ctx = "\n\n".join(open(f"{docs_dir}/{d}.txt").read() for d in x["context_ids"][:5])
                body = {"groundednessInput": {"metricSpec": {}, "instance": {"prediction": x["gen"]["answer"], "context": ctx}},
                        "autoraterConfig": {"autoraterModel": f"projects/{P}/locations/{loc}/publishers/google/models/{model}"}}
                req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
                    "Authorization": f"Bearer {tok}", "x-goog-user-project": P, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        print(f"evalsvc {model}@{loc}: OK", json.loads(r.read()).get("groundednessResult"))
                except urllib.error.HTTPError as e:
                    print(f"evalsvc {model}@{loc}: HTTP {e.code}", e.read().decode()[:160].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
