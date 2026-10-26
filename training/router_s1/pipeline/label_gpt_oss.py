#!/usr/bin/env python3
"""label_gpt_oss.py — label the golden-set messages with cross-family model on Vertex AI (MaaS).

Uses the Vertex OpenAI-compatible chat-completions endpoint with the caller's gcloud token, so
no OpenAI API key is involved and the data stays inside the GCP project. Same rubric as the
Gemini teacher (router_schema.py). 3 samples per message -> majority + agreement.
No third-party packages needed (urllib only).

  python label_gpt_oss.py golden_sheet_rows.json out.jsonl --project P [--region us-central1] [--probe]
"""
import argparse
import collections
import concurrent.futures as cf
import json
import re
import subprocess
import time
import urllib.request
from ticket_router.config.models import model_id
from training.router_s1.pipeline.router_schema import INTENTS, QUESTIONS, SENTIMENTS, URGENCY_LEVELS

def rubric():
    lines = []
    for qid, q in QUESTIONS.items():
        c = q["criteria"]
        opts = "; ".join(f"{k} = {v}" for k, v in c.items()) if isinstance(c, dict) else "; ".join(f"{i} = {v}" for i, v in enumerate(c))
        lines.append(f"- {qid}: {q['instructions']} ({opts})")
    return "\n".join(lines)

PROMPT = ("You label customer-support messages sent to an online retailer's support account.\n"
          "Answer four questions about the message:\n{rubric}\n"
          "Sarcasm counts as frustrated or angry. Polite wording with a hard deadline is urgent. "
          "Tokens like [ORDER], [EMAIL], [URL], @user are redactions. Judge the message only.\n"
          'Reply with ONLY a JSON object: {{"escalate": true|false, "sentiment": "...", "urgency": 0-3, "intent": "..."}}\n'
          "Message: ```{text}```")

def token():
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

def call(url, tok, model, text, seed):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT.format(rubric=rubric(), text=text)}],
            "temperature": 0.7, "seed": seed, "max_tokens": 2048, "reasoning_effort": "low"}
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            content = resp["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", content, re.S)
            v = json.loads(m.group(0))
            ok = (isinstance(v.get("escalate"), bool) and v.get("sentiment") in SENTIMENTS and v.get("intent") in INTENTS
                  and int(v.get("urgency", -1)) in range(URGENCY_LEVELS))
            if not ok: raise ValueError(f"bad labels: {v}")
            return {"escalate": v["escalate"], "sentiment": v["sentiment"], "urgency": int(v["urgency"]), "intent": v["intent"],
                    "usage": resp.get("usage", {})}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503) and attempt < 5: time.sleep(min(60, 2 ** attempt)); continue
            return {"error": f"{e.code} {msg}"}
        except Exception as e:
            if attempt < 2: continue
            return {"error": str(e)[:300]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rows"); ap.add_argument("out"); ap.add_argument("--project", required=True)
    ap.add_argument("--region", default="us-central1"); ap.add_argument("--model", default=model_id("cross_family_gpt_oss"))
    ap.add_argument("--samples", type=int, default=3); ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    host = "aiplatform.googleapis.com" if a.region == "global" else f"{a.region}-aiplatform.googleapis.com"
    url = f"https://{host}/v1beta1/projects/{a.project}/locations/{a.region}/endpoints/openapi/chat/completions"
    tok = token()
    rows = json.load(open(a.rows))
    if a.probe:
        print(json.dumps(call(url, tok, a.model, rows[0][1], 0))); return
    def one(row):
        cid, text = row
        votes = [call(url, tok, a.model, text, s) for s in range(a.samples)]
        ok = [v for v in votes if "error" not in v]
        maj = {q: (collections.Counter(str(v[q]) for v in ok).most_common(1)[0][0] if ok else "") for q in ("escalate", "sentiment", "urgency", "intent")}
        agree = {q: (sum(str(v[q]) == maj[q] for v in ok) / len(ok) if ok else 0) for q in maj}
        return {"case_id": cid, "majority": maj, "agreement": agree, "votes": votes}
    t0 = time.time(); out = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(one, rows), 1):
            out.append(r)
            if i % 50 == 0: print(f"  {i}/{len(rows)} in {time.time() - t0:.0f}s", flush=True)
    with open(a.out, "w") as fh:
        for r in out: fh.write(json.dumps(r) + "\n")
    errs = sum(1 for r in out for v in r["votes"] if "error" in v)
    tin = sum(v.get("usage", {}).get("prompt_tokens", 0) for r in out for v in r["votes"] if "error" not in v)
    tout = sum(v.get("usage", {}).get("completion_tokens", 0) for r in out for v in r["votes"] if "error" not in v)
    print(f"done {len(out)} rows, {errs} failed calls, tokens in={tin} out={tout}, {time.time() - t0:.0f}s -> {a.out}")

if __name__ == "__main__":
    main()
