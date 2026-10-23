#!/usr/bin/env python3
"""build_faq_corpus.py — synthesize the citable `faq-articles` corpus for Agent Search.

Pipeline (all PII-redacted inputs):
  1. Cluster the teacher's free-text `topic` per intent family into FAQ themes (Gemini 3.1 Pro).
  2. For each theme, show Gemini up to 15 real AmazonHelp threads (customer message + agent
     replies) and have it write ONE FAQ article for the simulated retailer "Global Retail Hub":
     title, 6-10 question paraphrases, answer, constraints, when-to-escalate.
     Policy facts come from one canonical SIMULATED_POLICY so articles never contradict each other.
  3. Write one .txt per article + Discovery Engine import metadata (JSONL).

Uses Vertex AI REST with the caller's gcloud token (urllib only).

  python build_faq_corpus.py --labels labels.jsonl --threads threads.jsonl --out out_dir --project P
"""
import argparse, collections, concurrent.futures as cf, json, pathlib, re, subprocess, time, urllib.error, urllib.request

SIMULATED_POLICY = """Global Retail Hub (simulated retailer) — canonical policy facts. Use ONLY these numbers.
- Membership: "Hub Plus" (paid; free 2-day shipping on eligible items, video/music streaming, e-book lending).
- Standard shipping: 4-6 business days; free over $35. Hub Plus: 2 business days on eligible items. Same-day in select cities.
- Delivery estimates are shown at checkout and in Your Orders; a package is "late" 1 business day after the latest estimate.
- Missing package marked delivered: wait until end of next day, check neighbors/safe places, then contact support for replacement or refund.
- Returns: 30 days from delivery for most items; opened electronics 14 days; "final sale" items not returnable. Free return label for defective/wrong items.
- Refunds: issued to original payment method within 3-5 business days after the return is received; gift card refunds within 24 hours.
- Damaged/defective/wrong item: report within 30 days for free replacement or refund; photos may be requested.
- Cancellations: possible until the order enters "Preparing for shipment"; otherwise refuse delivery or return it.
- Payments: declined payments retry once within 24 h; update the payment method in Your Orders. Duplicate/unauthorized charges: support investigates within 2 business days.
- Account security: support never asks for passwords or full card numbers; suspected phishing should be reported and the password changed.
- Third-party sellers: covered by the Hub Guarantee for items not received or not as described (claim within 90 days).
- Digital content (videos, e-books, apps): purchases are tied to the account; playback or sync issues -> check device registration and app updates first.
- Support channels: chat and phone 24/7; social media replies within 1 hour; never post order numbers or personal details publicly."""

INTENTS = ["order_status", "shipping_policy", "returns_refunds", "account_payment", "other"]

class Tok:
    def __init__(self): self.t, self.at = None, 0
    def __call__(self):
        if not self.t or time.time() - self.at > 2400:
            self.t = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip(); self.at = time.time()
        return self.t

def gemini(tok, project, prompt, schema, model="gemini-3.1-pro-preview", thinking="LOW"):
    url = f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0.4,
                                 "thinkingConfig": {"thinkingLevel": thinking}}}
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {tok()}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"] if not p.get("thought"))
            return json.loads(text), resp.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 5: time.sleep(min(60, 2 ** attempt + 1)); continue
            raise RuntimeError(f"{e.code} {e.read().decode()[:300]}")
        except (KeyError, json.JSONDecodeError):
            if attempt < 2: continue
            raise

THEME_SCHEMA = {"type": "object", "required": ["themes"], "properties": {"themes": {"type": "array", "items": {
    "type": "object", "required": ["theme_id", "title", "description", "topics"],
    "properties": {"theme_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
                   "topics": {"type": "array", "items": {"type": "string"}}}}}}}

ARTICLE_SCHEMA = {"type": "object", "required": ["title", "question_paraphrases", "answer_markdown", "constraints", "escalate_when"],
    "properties": {"title": {"type": "string"}, "question_paraphrases": {"type": "array", "items": {"type": "string"}},
                   "answer_markdown": {"type": "string"}, "constraints": {"type": "array", "items": {"type": "string"}},
                   "escalate_when": {"type": "array", "items": {"type": "string"}}}}

def thread_text(t, max_chars=900):
    lines = [f"{'Customer' if x['role'] == 'customer' else 'Agent'}: {x['text']}" for x in t["turns"]]
    return "\n".join(lines)[:max_chars]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True); ap.add_argument("--threads", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--project", required=True)
    ap.add_argument("--min-theme-size", type=int, default=6); ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--gcs-prefix", default="gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v1")
    a = ap.parse_args()
    tok = Tok(); out = pathlib.Path(a.out); (out / "docs").mkdir(parents=True, exist_ok=True)
    threads = {str(json.loads(l)["thread_id"]): json.loads(l) for l in open(a.threads)}

    msgs = collections.defaultdict(list)   # intent -> [(thread_id, topic)]
    for l in open(a.labels):
        r = json.loads(l)
        topics = [v.get("topic") for v in r["votes"] if v.get("topic")]
        if not topics: continue
        intent = max(r["gold"]["intent"]["probabilities"], key=r["gold"]["intent"]["probabilities"].get)
        msgs[intent].append((str(r["thread_id"]), topics[0].strip().lower()))
    usage = collections.Counter()

    # 1) cluster topics into themes per intent
    themes = []
    for intent in INTENTS:
        cnt = collections.Counter(t for _, t in msgs[intent])
        topic_list = "\n".join(f"{n}\t{t}" for t, n in cnt.most_common(700))
        k = "12-30" if intent == "other" else "6-18"
        res, u = gemini(tok, a.project,
            f"These are short issue topics (count<TAB>topic) from customer-support messages to an online retailer, "
            f"all labeled intent='{intent}'. Group them into {k} FAQ themes, each answerable by ONE help-center article. "
            f"Every topic must appear in exactly one theme; copy topic strings exactly. theme_id: short snake_case.\n\n{topic_list}",
            THEME_SCHEMA)
        for k_, v in u.items():
            if isinstance(v, int): usage[k_] += v
        t2theme = {}
        for th in res["themes"]:
            for t in th["topics"]: t2theme[t.strip().lower()] = th["theme_id"]
        for th in res["themes"]:
            members = [tid for tid, t in msgs[intent] if t2theme.get(t) == th["theme_id"]]
            themes.append({**th, "intent_family": intent, "thread_ids": members})
        print(f"{intent}: {len(res['themes'])} themes from {len(cnt)} topics / {len(msgs[intent])} messages", flush=True)
    json.dump(themes, open(out / "themes.json", "w"), indent=1)

    # 2) one article per sufficiently large theme
    todo = [t for t in themes if len(t["thread_ids"]) >= a.min_theme_size]
    def write(th):
        ex = [threads[t] for t in th["thread_ids"] if t in threads and any(x["role"] == "agent" for x in threads[t]["turns"])][:15]
        examples = "\n\n---\n".join(thread_text(t) for t in ex)
        prompt = (f"You write help-center FAQ articles for Global Retail Hub, a simulated online retailer.\n{SIMULATED_POLICY}\n\n"
                  f"Theme: {th['title']} — {th['description']} (intent family: {th['intent_family']}).\n"
                  f"Below are real, PII-redacted customer messages and support-agent replies on this theme (from another retailer; "
                  f"use them for the customer's language and the troubleshooting steps agents used, NOT for brand names or policy numbers).\n\n"
                  f"{examples}\n\nWrite ONE article:\n- title: a customer question, e.g. 'What should I do if my package is marked delivered but I can't find it?'\n"
                  f"- question_paraphrases: 6-10 ways customers actually ask this, informal, varied\n"
                  f"- answer_markdown: 120-300 words, direct answer first, then numbered steps; use only the policy facts above; "
                  f"never ask for passwords or full card numbers; brand is 'Global Retail Hub', membership is 'Hub Plus'\n"
                  f"- constraints: exceptions and limits (from the policy)\n- escalate_when: 2-4 situations that need a human agent")
        art, u = gemini(tok, a.project, prompt, ARTICLE_SCHEMA)
        return th, art, u, len(ex)
    articles = []
    with cf.ThreadPoolExecutor(a.workers) as pool:
        for th, art, u, n_ex in pool.map(write, todo):
            for k_, v in u.items():
                if isinstance(v, int): usage[k_] += v
            doc_id = re.sub(r"[^a-z0-9-]", "-", f"faq-{th['intent_family']}-{th['theme_id']}".lower().replace("_", "-"))[:120]
            body = (f"# {art['title']}\n\nAlso asked as:\n" + "\n".join(f"- {q}" for q in art["question_paraphrases"]) +
                    f"\n\n{art['answer_markdown']}\n\n## Limits and exceptions\n" + "\n".join(f"- {c}" for c in art["constraints"]) +
                    "\n\n## When we connect you with a specialist\n" + "\n".join(f"- {c}" for c in art["escalate_when"]) +
                    "\n\n_Global Retail Hub is a simulated retailer; policy details are illustrative._\n")
            (out / "docs" / f"{doc_id}.txt").write_text(body, encoding="utf-8")
            articles.append({"id": doc_id, "structData": {"title": art["title"], "intent_family": th["intent_family"],
                             "theme": th["title"], "version": 1, "n_source_messages": len(th["thread_ids"]),
                             "n_example_threads": n_ex, "simulated_policy": True, "reviewed": False},
                             "content": {"mimeType": "text/plain", "uri": f"{a.gcs_prefix}/docs/{doc_id}.txt"}})
    with open(out / "faq_articles_import.jsonl", "w") as fh:
        for x in articles: fh.write(json.dumps(x) + "\n")
    print(f"articles: {len(articles)} (themes {len(themes)}, skipped small {len(themes) - len(todo)}); usage {dict(usage)}")

if __name__ == "__main__":
    main()
