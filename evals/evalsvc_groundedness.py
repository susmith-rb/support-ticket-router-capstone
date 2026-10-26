#!/usr/bin/env python3
"""evalsvc_groundedness.py — Gen AI Evaluation Service pointwise groundedness with an explicit autorater.

Uses a PointwiseMetric with a groundedness rubric (1-5) and an explicit autorater configured
via config/models.yaml (role: evalsvc_autorater).

  python evalsvc_groundedness.py clean_eval_dir docs_dir [--limit N]
"""
import argparse
import concurrent.futures as cf
import json
import time
import urllib.error
import urllib.request

from ticket_router.common.auth import get_access_token
from ticket_router.config.models import model_id

P = "susmith-fde-ticket-router"

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


def get_token():
    return get_access_token()


def build_request_body(prediction, context, model=None, project=P, loc="global"):
    """Construct evaluateInstances body for pointwise groundedness."""
    resolved_model = model or model_id("evalsvc_autorater")
    return {
        "pointwiseMetricInput": {
            "metricSpec": {"metricPromptTemplate": TEMPLATE},
            "instance": {"jsonInstance": json.dumps({"prediction": prediction, "context": context})},
        },
        "autoraterConfig": {
            "autoraterModel": f"projects/{project}/locations/{loc}/publishers/google/models/{resolved_model}"
        },
    }


def evaluate_single_instance(url, tok, project, body, question, max_retries=5, sleep_fn=time.sleep):
    """Call evaluateInstances for one instance, retrying on 429, 500, 503, and timeouts."""
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {tok}", "x-goog-user-project": project, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                res = json.loads(r.read()).get("pointwiseMetricResult", {})
                return {"question": question, "score": res.get("score"), "explanation": (res.get("explanation") or "")[:300]}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503) and attempt < max_retries - 1:
                sleep_fn(5 * (attempt + 1))
                continue
            return {"question": question, "error": f"{e.code} {msg}"}
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt < max_retries - 1:
                sleep_fn(5 * (attempt + 1))
                continue
            return {"question": question, "error": f"timeout/conn: {e}"}
    return {"question": question, "error": "retries exhausted"}


def aggregate_scores(res, model=None):
    """Aggregate pointwise scores, error counts, and distribution."""
    sc = [r["score"] for r in res if r.get("score") is not None]
    dist = {s: sc.count(s) for s in sorted(set(sc))}
    rep = {
        "scored": len(sc),
        "errors": len(res) - len(sc),
        "mean_1to5": round(sum(sc) / len(sc), 2) if sc else None,
        "share_score_ge4": round(sum(1 for s in sc if s >= 4) / len(sc), 3) if sc else None,
        "distribution": dist,
        "first_error": next((r["error"] for r in res if "error" in r), None),
    }
    if model:
        rep["autorater"] = model
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("w")
    ap.add_argument("docs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=model_id("evalsvc_autorater"))
    ap.add_argument("--loc", default="global")
    a = ap.parse_args()

    tok = get_token()
    host = "aiplatform.googleapis.com" if a.loc == "global" else f"{a.loc}-aiplatform.googleapis.com"
    url = f"https://{host}/v1/projects/{P}/locations/{a.loc}:evaluateInstances"

    ans = [json.loads(l) for l in open(f"{a.w}/answers.jsonl")]
    if a.limit:
        ans = ans[: a.limit]

    def ev(x):
        ctx = "\n\n".join(open(f"{a.docs}/{d}.txt").read() for d in x["context_ids"][:5])
        body = build_request_body(prediction=x["gen"]["answer"], context=ctx, model=a.model, project=P, loc=a.loc)
        return evaluate_single_instance(url, tok, P, body, x["question"])

    with cf.ThreadPoolExecutor(4) as ex:
        res = list(ex.map(ev, ans))

    with open(f"{a.w}/evalsvc_pointwise.jsonl", "w") as fh:
        for r in res:
            fh.write(json.dumps(r) + "\n")

    summary = aggregate_scores(res, model=a.model)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
