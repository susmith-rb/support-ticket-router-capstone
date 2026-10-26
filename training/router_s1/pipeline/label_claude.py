#!/usr/bin/env python3
"""label_claude.py — label the golden-set messages with Claude (Opus / Sonnet) on Vertex AI.

Anthropic Messages API via Vertex `:rawPredict`, caller's gcloud token, extended thinking with a
high budget. Same rubric as the Gemini teacher (router_schema.py). Default 1 sample per message
(thinking forces temperature 1; use --samples 3 for votes). urllib only.

  python label_claude.py golden_sheet_rows.json out.jsonl --project P \
      [--region global] [--thinking 8000] [--samples 1] [--limit 5]
"""
import argparse, collections, concurrent.futures as cf, json, pathlib, re, subprocess, sys, time, urllib.error, urllib.request
sys.path.insert(0, __import__("os").path.dirname(__file__))

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.models import model_id

from label_gpt_oss import PROMPT, rubric
from router_schema import SENTIMENTS, INTENTS, URGENCY_LEVELS

def token():
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

def call(url, tok, text, thinking, max_tokens, effort=None):
    body = {"anthropic_version": "vertex-2023-10-16", "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROMPT.format(rubric=rubric(), text=text)}]}
    if effort:   # Claude 5.5-generation models: adaptive thinking controlled by effort
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": effort}
    elif thinking:
        body["thinking"] = {"type": "enabled", "budget_tokens": thinking}
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {tok()}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            textout = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            v = json.loads(re.search(r"\{.*\}", textout, re.S).group(0))
            ok = (isinstance(v.get("escalate"), bool) and v.get("sentiment") in SENTIMENTS and v.get("intent") in INTENTS
                  and int(v.get("urgency", -1)) in range(URGENCY_LEVELS))
            if not ok: raise ValueError(f"bad labels: {v}")
            return {"escalate": v["escalate"], "sentiment": v["sentiment"], "urgency": int(v["urgency"]),
                    "intent": v["intent"], "usage": resp.get("usage", {})}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 503, 529) and attempt < 5: time.sleep(min(60, 2 ** attempt + 1)); continue
            return {"error": f"{e.code} {msg}"}
        except Exception as e:
            if attempt < 2: continue
            return {"error": str(e)[:300]}

class Tok:
    def __init__(self): self.t, self.at = token(), time.time()
    def __call__(self):
        if time.time() - self.at > 2400: self.t, self.at = token(), time.time()
        return self.t

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rows"); ap.add_argument("out"); ap.add_argument("--project", required=True)
    ap.add_argument("--model", default=model_id("cross_family_claude")); ap.add_argument("--region", default="global")
    ap.add_argument("--thinking", type=int, default=8000); ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--effort", default="high", help="adaptive-thinking effort for 5.5-gen models; '' to use --thinking budget")
    ap.add_argument("--workers", type=int, default=8); ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    host = "aiplatform.googleapis.com" if a.region == "global" else f"{a.region}-aiplatform.googleapis.com"
    url = f"https://{host}/v1/projects/{a.project}/locations/{a.region}/publishers/anthropic/models/{a.model}:rawPredict"
    tok = Tok(); rows = json.load(open(a.rows))
    if a.limit: rows = rows[: a.limit]
    def one(row):
        cid, text = row
        votes = [call(url, tok, text, a.thinking, 16000 if a.effort else a.thinking + 1024, a.effort or None) for _ in range(a.samples)]
        ok = [v for v in votes if "error" not in v]
        maj = {q: (collections.Counter(str(v[q]) for v in ok).most_common(1)[0][0] if ok else "") for q in ("escalate", "sentiment", "urgency", "intent")}
        agree = {q: (sum(str(v[q]) == maj[q] for v in ok) / len(ok) if ok else 0) for q in maj}
        return {"case_id": cid, "model": a.model, "majority": maj, "agreement": agree, "votes": votes}
    t0 = time.time(); out = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(one, rows), 1):
            out.append(r)
            if i % 25 == 0: print(f"  {i}/{len(rows)} in {time.time() - t0:.0f}s", flush=True)
    with open(a.out, "w") as fh:
        for r in out: fh.write(json.dumps(r) + "\n")
    ok = [v for r in out for v in r["votes"] if "error" not in v]
    errs = sum(1 for r in out for v in r["votes"] if "error" in v)
    tin = sum(v["usage"].get("input_tokens", 0) for v in ok); tout = sum(v["usage"].get("output_tokens", 0) for v in ok)
    print(f"done {len(out)} rows, ok calls {len(ok)}, failed {errs}, tokens in={tin} out={tout} "
          f"(per call in={tin / max(1, len(ok)):.0f} out={tout / max(1, len(ok)):.0f}), {time.time() - t0:.0f}s -> {a.out}")
    if errs: print("first error:", next(v["error"] for r in out for v in r["votes"] if "error" in v))

if __name__ == "__main__":
    main()
