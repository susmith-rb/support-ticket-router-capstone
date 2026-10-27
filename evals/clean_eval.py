#!/usr/bin/env python3
"""clean_eval.py — comparable-to-baseline RAG eval on clean, well-formed questions.

A. Clean-question retrieval: Claude Sonnet 5.5 writes N fresh customer questions per FAQ article
   (different model family from the article writer; told not to reuse the article's paraphrases).
   Agent Search (best config: relevanceThreshold LOWEST) -> recall@1/3/5/10, MRR.
B. Answer groundedness: Gemini Flash answers each question from the top-5 retrieved articles with
   mandatory citations (doc_id + verbatim quote). Scored by
     - deterministic checks: citation present, citation precision (quote is verbatim in the cited
       article), cited article == the article the question was written from, abstention rate
     - Gen AI Evaluation Service `groundedness` (pointwise, 0/1) when available
     - Claude Opus 5.5 atomic-claim judge: % of answer claims supported by the retrieved articles,
       plus a 1-5 faithfulness score (the same family of metric as the baseline deck's "grounding")

All calls use ADC via ticket_router.common.auth.get_access_token via urllib. Each stage
caches to --out so it can be resumed.

  python clean_eval.py --docs faq/docs --import faq/faq_articles_import.jsonl --out clean_eval/ [--per-article 2]
"""
import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import time
import urllib.error
import urllib.request

from ticket_router.common.auth import get_access_token
from ticket_router.config.models import model_id
from ticket_router.retrieval.de_endpoint import data_store_name, search_url

P = "susmith-fde-ticket-router"
VERTEX_LOC = "global"
DEFAULT_LOCATION = "global"
DS = data_store_name(P, "faq-articles", location=DEFAULT_LOCATION)
SEARCH = search_url(P, "support-kb-search", location=DEFAULT_LOCATION)


class Tok:
    def __call__(self):
        return get_access_token()


tok = Tok()


def post(url, body, extra=None, retries=6, timeout=120):
    h = {"Authorization": f"Bearer {tok()}", "Content-Type": "application/json", "x-goog-user-project": P, **(extra or {})}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(min(60, 4 * (attempt + 1)))
                continue
            if isinstance(e, urllib.error.HTTPError):
                msg = e.read().decode()[:400]
                raise RuntimeError(f"HTTP {e.code}: {msg}") from e
            raise


def claude(model, prompt, effort="high", max_tokens=8000):
    url = f"https://aiplatform.googleapis.com/v1/projects/{P}/locations/{VERTEX_LOC}/publishers/anthropic/models/{model}:rawPredict"
    body = {"anthropic_version": "vertex-2023-10-16", "max_tokens": max_tokens, "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort}, "messages": [{"role": "user", "content": prompt}]}
    r = post(url, body)
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    return json.loads(re.search(r"\{.*\}|\[.*\]", text, re.S).group(0)), r.get("usage", {})


def gemini(model, prompt, schema):
    url = f"https://aiplatform.googleapis.com/v1/projects/{P}/locations/{VERTEX_LOC}/publishers/google/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.2}}
    r = post(url, body)
    text = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"] if not p.get("thought"))
    return json.loads(text), r.get("usageMetadata", {})


def search(q):
    r = post(SEARCH, {"query": q, "pageSize": 10, "relevanceThreshold": "LOWEST", "dataStoreSpecs": [{"dataStore": DS}]})
    time.sleep(0.7)
    return [x["document"]["id"] for x in r.get("results", [])]


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>]", "", s)).strip().lower()


def is_verbatim_citation(quote, doc_text):
    """Check if the quote appears verbatim (modulo light formatting/spacing) in doc_text."""
    if not quote or not doc_text:
        return False
    return norm(quote) in norm(doc_text)


ANSWER_SCHEMA = {"type": "object", "required": ["answer", "citations", "grounded", "escalate"], "properties": {
    "answer": {"type": "string"}, "grounded": {"type": "boolean"}, "escalate": {"type": "boolean"},
    "citations": {"type": "array", "items": {"type": "object", "required": ["doc_id", "quote"],
                  "properties": {"doc_id": {"type": "string"}, "quote": {"type": "string"}}}}}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True)
    ap.add_argument("--import", dest="imp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--location", default="global", help="Discovery Engine location (default: global)")
    ap.add_argument("--per-article", type=int, default=2)
    ap.add_argument("--gen-model", default=model_id("answer"))
    ap.add_argument("--qmodel", default=model_id("question_gen"))
    ap.add_argument("--judge", default=model_id("judge"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--gold-map", default=None, help="path to gold map JSON (old_id -> new_id)")
    ap.add_argument("--skip-judge", action="store_true", help="skip Eval Service and Claude judge evaluation")
    ap.add_argument("--reuse-questions", nargs="?", const=True, default=None,
                    help="reuse existing questions.jsonl (path or true to use --out/questions.jsonl)")
    a = ap.parse_args()
    global SEARCH, DS
    SEARCH = search_url(P, "support-kb-search", location=a.location)
    DS = data_store_name(P, "faq-articles", location=a.location)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = [json.loads(l)["id"] for l in open(a.imp)]
    docs = {i: (pathlib.Path(a.docs) / f"{i}.txt").read_text(encoding="utf-8") for i in ids}
    usage = {"qgen": [0, 0], "answer": [0, 0], "judge": [0, 0]}

    gold_map = json.load(open(a.gold_map)) if a.gold_map else {}

    # ---- A1. question generation
    qf = out / "questions.jsonl"
    if a.reuse_questions:
        if isinstance(a.reuse_questions, str):
            src_q = pathlib.Path(a.reuse_questions)
            if not src_q.exists():
                raise FileNotFoundError(f"Questions file not found: {a.reuse_questions}")
            if src_q.resolve() != qf.resolve():
                qf.write_text(src_q.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            if not qf.exists():
                candidates = [pathlib.Path("rag/results/clean_eval/questions.jsonl"), pathlib.Path("clean_eval/questions.jsonl")]
                found = False
                for c in candidates:
                    if c.exists():
                        qf.write_text(c.read_text(encoding="utf-8"), encoding="utf-8")
                        found = True
                        break
                if not found:
                    raise FileNotFoundError(f"No existing questions.jsonl found to reuse in {out}")
    elif not qf.exists():
        def gen(doc_id):
            prompt = (f"Here is a help-center article from an online retailer:\n\n{docs[doc_id]}\n\n"
                      f"Write {a.per_article} NEW questions a real customer might type into a support chat that this article answers. "
                      "Rules: well-formed and natural; do NOT reuse or lightly edit the title or any line under 'Also asked as'; "
                      "vary the wording and the specific situation; no brand names. "
                      'Return JSON: {"questions": ["...", "..."]}')
            res, u = claude(a.qmodel, prompt, effort="low", max_tokens=2000)
            return doc_id, res["questions"][: a.per_article], u
        with cf.ThreadPoolExecutor(a.workers) as ex, open(qf, "w") as fh:
            for doc_id, qs, u in ex.map(gen, ids):
                usage["qgen"][0] += u.get("input_tokens", 0)
                usage["qgen"][1] += u.get("output_tokens", 0)
                for q in qs:
                    fh.write(json.dumps({"question": q, "source_doc": doc_id}) + "\n")
    Q = [json.loads(l) for l in open(qf)]
    print(f"questions: {len(Q)}", flush=True)

    # ---- A2. retrieval
    rf = out / "retrieval.jsonl"
    if not rf.exists():
        with open(rf, "w") as fh:
            for q in Q:
                target = gold_map.get(q["source_doc"], q["source_doc"]) if gold_map else q["source_doc"]
                top = search(q["question"])
                if isinstance(target, (list, tuple, set)):
                    matched_ranks = [top.index(t) + 1 for t in target if t in top]
                    rank = min(matched_ranks) if matched_ranks else None
                else:
                    rank = top.index(target) + 1 if target in top else None
                fh.write(json.dumps({**q, "top": top, "rank": rank, "effective_gold": target}) + "\n")
    RET = [json.loads(l) for l in open(rf)]
    n = len(RET)
    rec = {k: round(sum(1 for r in RET if r["rank"] and r["rank"] <= k) / n, 3) for k in (1, 3, 5, 10)}
    mrr = round(sum(1 / r["rank"] for r in RET if r["rank"]) / n, 3)
    zero = sum(1 for r in RET if not r["top"])
    print(f"A. clean-question retrieval: n={n} recall={rec} MRR@10={mrr} zero-result={zero}", flush=True)

    # ---- B1. grounded answer generation
    af = out / "answers.jsonl"
    if not af.exists():
        def answer(r):
            ctx = "\n\n".join(f"<doc id=\"{d}\">\n{docs[d]}\n</doc>" for d in r["top"][:5] if d in docs)
            prompt = ("You are Global Retail Hub's support assistant. Answer the customer ONLY from the documents below. "
                      "Every factual statement must be supported by a citation: doc_id plus a short VERBATIM quote copied from that doc. "
                      "If the documents don't answer the question, set grounded=false, escalate=true and say a specialist will help. "
                      "Keep the answer under 120 words, friendly and direct.\n\n"
                      f"{ctx}\n\nCustomer: {r['question']}")
            res, u = gemini(a.gen_model, prompt, ANSWER_SCHEMA)
            return r, res, u, ctx
        with cf.ThreadPoolExecutor(a.workers) as ex, open(af, "w") as fh:
            for r, res, u, _ctx in ex.map(answer, RET):
                usage["answer"][0] += u.get("promptTokenCount", 0)
                usage["answer"][1] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
                fh.write(json.dumps({**r, "gen": res, "context_ids": r["top"][:5]}) + "\n")
    ANS = [json.loads(l) for l in open(af)]

    # ---- B2. deterministic checks
    cit_total = cit_ok = with_cit = src_cited = abstain = 0
    for x in ANS:
        g = x["gen"]
        cits = g.get("citations", [])
        abstain += bool(g.get("escalate")) or not g.get("grounded", True)
        with_cit += bool(cits)
        target = gold_map.get(x["source_doc"], x["source_doc"]) if gold_map else x["source_doc"]
        if isinstance(target, (list, tuple, set)):
            src_cited += any(c["doc_id"] in target for c in cits)
        else:
            src_cited += any(c["doc_id"] == target for c in cits)
        for c in cits:
            cit_total += 1
            cit_ok += c["doc_id"] in docs and is_verbatim_citation(c["quote"], docs[c["doc_id"]])
    det = {"answers": len(ANS), "answers_with_citation": round(with_cit / len(ANS), 3),
           "citation_precision_verbatim": round(cit_ok / max(1, cit_total), 3), "citations": cit_total,
           "cites_source_article": round(src_cited / len(ANS), 3), "abstained_or_escalated": round(abstain / len(ANS), 3)}
    print("B. deterministic:", det, flush=True)

    # ---- B3. Gen AI Evaluation Service groundedness (pointwise)
    if a.skip_judge:
        evalsvc = {"skipped": True}
        print("B. Gen AI Evaluation Service groundedness: skipped (--skip-judge)", flush=True)
    else:
        ef = out / "evalsvc.jsonl"
        if not ef.exists():
            url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{P}/locations/us-central1:evaluateInstances"
            def ev(x):
                ctx = "\n\n".join(docs[d] for d in x["context_ids"] if d in docs)
                try:
                    r = post(url, {"groundednessInput": {"metricSpec": {}, "instance": {"prediction": x["gen"]["answer"], "context": ctx}}})
                    return {"score": r.get("groundednessResult", {}).get("score"), "explanation": r.get("groundednessResult", {}).get("explanation", "")[:300]}
                except Exception as e:
                    return {"error": str(e)[:300]}
            with cf.ThreadPoolExecutor(4) as ex, open(ef, "w") as fh:
                for x, r in zip(ANS, ex.map(ev, ANS)):
                    fh.write(json.dumps({"question": x["question"], **r}) + "\n")
        EV = [json.loads(l) for l in open(ef)]
        sc = [e["score"] for e in EV if e.get("score") is not None]
        evalsvc = {"scored": len(sc), "errors": len(EV) - len(sc), "groundedness_mean": round(sum(sc) / len(sc), 3) if sc else None,
                   "first_error": next((e["error"] for e in EV if "error" in e), None)}
        print("B. Gen AI Evaluation Service groundedness:", evalsvc, flush=True)

    # ---- B4. cross-family atomic-claim judge
    if a.skip_judge:
        judge_rep = {"skipped": True}
        print("B. Opus atomic-claim judge: skipped (--skip-judge)", flush=True)
    else:
        jf = out / "judge.jsonl"
        if not jf.exists():
            def judge(x):
                ctx = "\n\n".join(f"<doc id=\"{d}\">\n{docs[d]}\n</doc>" for d in x["context_ids"] if d in docs)
                prompt = ("You are grading whether a support answer is grounded in the provided documents.\n"
                          "1) Split the ANSWER into atomic factual claims (ignore greetings/empathy phrases).\n"
                          "2) For each claim, decide supported=true only if the documents state it (paraphrase OK, no outside knowledge).\n"
                          "3) faithfulness 1-5: 5 = every claim supported; 3 = mostly supported, one minor unsupported detail; "
                          "1 = any material policy claim not in the documents.\n"
                          f"{ctx}\n\nQUESTION: {x['question']}\nANSWER: {x['gen']['answer']}\n\n"
                          'Return JSON: {"claims": [{"claim": "...", "supported": true}], "faithfulness": 1-5}')
                try:
                    res, u = claude(a.judge, prompt, effort="high")
                    return res, u
                except Exception as e:
                    return {"error": str(e)[:300]}, {}
            with cf.ThreadPoolExecutor(a.workers) as ex, open(jf, "w") as fh:
                for x, (res, u) in zip(ANS, ex.map(judge, ANS)):
                    usage["judge"][0] += u.get("input_tokens", 0)
                    usage["judge"][1] += u.get("output_tokens", 0)
                    fh.write(json.dumps({"question": x["question"], **res}) + "\n")
        JD = [json.loads(l) for l in open(jf)]
        claims = [c for j in JD for c in j.get("claims", [])]
        faith = [j["faithfulness"] for j in JD if isinstance(j.get("faithfulness"), (int, float))]
        judge_rep = {"judged": len(faith), "claims": len(claims),
                     "claim_support_rate": round(sum(1 for c in claims if c.get("supported")) / max(1, len(claims)), 3),
                     "faithfulness_mean": round(sum(faith) / max(1, len(faith)), 2),
                     "answers_fully_supported": round(sum(1 for j in JD if j.get("claims") and all(c.get("supported") for c in j["claims"])) / max(1, len(JD)), 3)}
        print("B. Opus atomic-claim judge:", judge_rep, flush=True)

    rep = {"A_retrieval": {"n": n, **{f"recall@{k}": v for k, v in rec.items()}, "mrr@10": mrr, "zero_result": zero},
           "B_deterministic": det, "B_evalsvc_groundedness": evalsvc, "B_judge": judge_rep, "usage_this_run": usage,
           "models": {"questions": a.qmodel, "answers": a.gen_model, "judge": a.judge}}
    json.dump(rep, open(out / "report.json", "w"), indent=1)
    misses = [r for r in RET if not r["rank"] or r["rank"] > 5][:10]
    json.dump(misses, open(out / "retrieval_misses.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
