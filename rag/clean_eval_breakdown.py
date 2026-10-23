#!/usr/bin/env python3
"""clean_eval_breakdown.py — abstention vs retrieval rank, unsupported claims, and an Eval Service retry with an explicit autorater."""
import json, sys, subprocess, urllib.request, urllib.error
W = sys.argv[1]
ans = [json.loads(l) for l in open(f"{W}/answers.jsonl")]
jd = [json.loads(l) for l in open(f"{W}/judge.jsonl")]
def esc(x): return bool(x["gen"].get("escalate")) or not x["gen"].get("grounded", True)
hit = [x for x in ans if x["rank"] and x["rank"] <= 5]; miss = [x for x in ans if not x["rank"] or x["rank"] > 5]
print(f"abstained when source article in top-5: {sum(map(esc, hit))}/{len(hit)}; when NOT in top-5: {sum(map(esc, miss))}/{len(miss)}")
answered = [x for x in ans if not esc(x)]
print(f"answered (not escalated): {len(answered)}; of those, cite the source article: {sum(1 for x in answered if any(c['doc_id']==x['source_doc'] for c in x['gen']['citations']))}")
print("\nsample abstentions where source WAS retrieved:")
for x in [x for x in hit if esc(x)][:4]: print("  -", x["question"][:120], "| rank", x["rank"], "|", x["gen"]["answer"][:120])
print("\nunsupported claims (judge):")
k = 0
for j in jd:
    for c in j.get("claims", []):
        if not c.get("supported") and k < 8: print("  -", c["claim"][:150]); k += 1
# Eval Service retry with explicit autorater on 3 answers
P = "susmith-fde-ticket-router"
tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
docs_dir = sys.argv[2]
for model in ("gemini-3.8-flash", "gemini-3.1-pro-preview"):
    for loc in ("global", "us-central1"):
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{P}/locations/{loc}:evaluateInstances"
        x = ans[0]; ctx = "\n\n".join(open(f"{docs_dir}/{d}.txt").read() for d in x["context_ids"][:5])
        body = {"groundednessInput": {"metricSpec": {}, "instance": {"prediction": x["gen"]["answer"], "context": ctx}},
                "autoraterConfig": {"autoraterModel": f"projects/{P}/locations/{loc}/publishers/google/models/{model}"}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {tok}", "x-goog-user-project": P, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r: print(f"evalsvc {model}@{loc}: OK", json.loads(r.read()).get("groundednessResult"))
        except urllib.error.HTTPError as e: print(f"evalsvc {model}@{loc}: HTTP {e.code}", e.read().decode()[:160].replace("\n", " "))
