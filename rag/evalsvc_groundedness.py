#!/usr/bin/env python3
"""evalsvc_groundedness.py — Gen AI Evaluation Service pointwise groundedness with an explicit autorater.

The built-in `groundedness` metric uses a default autorater (gemini-2.5-flash) that isn't available in
this project and doesn't accept autoraterConfig, so this uses a PointwiseMetric with a groundedness
rubric (1-5) and autorater gemini-3.8-flash.

  python evalsvc_groundedness.py clean_eval_dir docs_dir [--limit N]
"""
import argparse, concurrent.futures as cf, json, subprocess, time, urllib.error, urllib.request
ap = argparse.ArgumentParser(); ap.add_argument("w"); ap.add_argument("docs"); ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--model", default="gemini-3.8-flash"); ap.add_argument("--loc", default="global")
a = ap.parse_args()
P = "susmith-fde-ticket-router"
tok = subprocess.check_output(["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
host = "aiplatform.googleapis.com" if a.loc == "global" else f"{a.loc}-aiplatform.googleapis.com"
url = f"https://{host}/v1/projects/{P}/locations/{a.loc}:evaluateInstances"
TEMPLATE = """# Instruction
You are an expert evaluator. Rate how well the AI-generated response is grounded in the provided context.
# Evaluation
## Metric Definition
Groundedness: every factual claim in the response is supported by the context; no outside facts or invented policy.
## Rating Rubric
5: Fully grounded - every claim is supported by the context.
4: Mostly grounded - one minor detail is an inference not stated in the context.
3: Partially grounded - some claims are supported, at least one material claim is not.
2: Mostly ungrounded - most claims are not in the context.
1: Ungrounded - contradicts the context or invents policy.
## Evaluation Steps
STEP 1: List the factual claims in the response. STEP 2: Check each against the context. STEP 3: Assign a score.
# User Inputs and AI-generated Response
## Context
{context}
## AI-generated Response
{prediction}"""
ans = [json.loads(l) for l in open(f"{a.w}/answers.jsonl")]
if a.limit: ans = ans[: a.limit]
def ev(x):
    ctx = "\n\n".join(open(f"{a.docs}/{d}.txt").read() for d in x["context_ids"][:5])
    body = {"pointwiseMetricInput": {"metricSpec": {"metricPromptTemplate": TEMPLATE},
                                     "instance": {"jsonInstance": json.dumps({"prediction": x["gen"]["answer"], "context": ctx})}},
            "autoraterConfig": {"autoraterModel": f"projects/{P}/locations/{a.loc}/publishers/google/models/{a.model}"}}
    for attempt in range(5):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {tok}", "x-goog-user-project": P, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                res = json.loads(r.read()).get("pointwiseMetricResult", {})
                return {"question": x["question"], "score": res.get("score"), "explanation": (res.get("explanation") or "")[:300]}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503) and attempt < 4: time.sleep(5 * (attempt + 1)); continue
            return {"question": x["question"], "error": f"{e.code} {msg}"}
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt < 4: time.sleep(5 * (attempt + 1)); continue
            return {"question": x["question"], "error": f"timeout/conn: {e}"}
    return {"question": x["question"], "error": "retries exhausted"}
with cf.ThreadPoolExecutor(4) as ex: res = list(ex.map(ev, ans))
with open(f"{a.w}/evalsvc_pointwise.jsonl", "w") as fh:
    for r in res: fh.write(json.dumps(r) + "\n")
sc = [r["score"] for r in res if r.get("score") is not None]
dist = {s: sc.count(s) for s in sorted(set(sc))}
print(json.dumps({"autorater": a.model, "scored": len(sc), "errors": len(res) - len(sc), "mean_1to5": round(sum(sc) / len(sc), 2) if sc else None,
                  "share_score_ge4": round(sum(1 for s in sc if s >= 4) / len(sc), 3) if sc else None, "distribution": dist,
                  "first_error": next((r["error"] for r in res if "error" in r), None)}, indent=1))
