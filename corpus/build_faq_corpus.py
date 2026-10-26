#!/usr/bin/env python3
"""build_faq_corpus.py — synthesize the citable `faq-articles` corpus for Agent Search.

Pipeline (all PII-redacted inputs):
  1. Cluster the teacher's free-text `topic` per intent family into FAQ themes,
     or load an existing themes file via `--themes`.
  2. For each theme, write ONE FAQ article for the simulated retailer "Global Retail Hub":
     title, 6-10 question paraphrases, answer, constraints, when-to-escalate.
     Policy facts come from one canonical SIMULATED_POLICY so articles never contradict each other.
     Supports `--reuse-from <v1 docs dir>` to copy unchanged articles verbatim (byte-identical)
     and only synthesize new/modified articles.
     Enforces strict policy number checking with automatic lint retry via `publish_faq.lint`.
  3. Write one .txt per article + Discovery Engine import metadata (JSONL).

Uses Vertex AI REST with the caller's gcloud token (urllib only).

  python build_faq_corpus.py --labels labels.jsonl --threads threads.jsonl --out out_dir --project P
  python build_faq_corpus.py --themes themes_v2.json --threads threads.jsonl --out out_dir --project P --reuse-from scratch/rag/faq/docs
"""

import argparse
import collections
import concurrent.futures as cf
import datetime
import hashlib
import json
import pathlib
import re
import time
import urllib.error
import urllib.request

from ticket_router.common.auth import get_access_token
from ticket_router.config.models import model_id

POLICY_VERSION = "policy-v1"  # bump when any fact below changes (TDD §6.6.2); publish_faq.py lint checks articles against it
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

def get_lint_fn():
    """Lazily import publish_faq.lint to avoid circular import when build_faq_corpus is __main__."""
    try:
        from corpus.publish_faq import lint
        return lint
    except ImportError:
        from publish_faq import lint
        return lint


class Tok:
    def __call__(self):
        return get_access_token()


def doc_id_for_theme(theme_dict_or_intent, theme_id=None):
    """Derive canonical doc_id `faq-<intent>-<slug>` from theme dict or intent + theme_id."""
    if isinstance(theme_dict_or_intent, dict):
        intent = theme_dict_or_intent.get("intent_family", "")
        tid = theme_dict_or_intent.get("theme_id", "")
    else:
        intent = theme_dict_or_intent
        tid = theme_id
    raw = f"faq-{intent}-{tid}".lower().replace("_", "-")
    return re.sub(r"[^a-z0-9-]", "-", raw)[:120]


def gemini(tok, project, prompt, schema, model=None, thinking="LOW"):
    resolved_model = model or model_id("corpus_author")
    url = f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{resolved_model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.4,
            "thinkingConfig": {"thinkingLevel": thinking},
        },
    }
    for attempt in range(6):
        try:
            bearer = tok() if callable(tok) else tok
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            text = "".join(
                p.get("text", "") for p in resp["candidates"][0]["content"]["parts"] if not p.get("thought")
            )
            return json.loads(text), resp.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 5:
                time.sleep(min(60, 2**attempt + 1))
                continue
            raise RuntimeError(f"{e.code} {e.read().decode()[:300]}")
        except (KeyError, json.JSONDecodeError):
            if attempt < 2:
                continue
            raise


THEME_SCHEMA = {
    "type": "object",
    "required": ["themes"],
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["theme_id", "title", "description", "topics"],
                "properties": {
                    "theme_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

ARTICLE_SCHEMA = {
    "type": "object",
    "required": [
        "title",
        "question_paraphrases",
        "answer_markdown",
        "constraints",
        "escalate_when",
    ],
    "properties": {
        "title": {"type": "string"},
        "question_paraphrases": {"type": "array", "items": {"type": "string"}},
        "answer_markdown": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "escalate_when": {"type": "array", "items": {"type": "string"}},
    },
}


def thread_text(t, max_chars=900):
    lines = [f"{'Customer' if x.get('role') == 'customer' else 'Agent'}: {x.get('text', '')}" for x in t.get("turns", [])]
    return "\n".join(lines)[:max_chars]


def build_article_body(art):
    """Format structured article response into canonical markdown doc body."""
    return (
        f"# {art['title']}\n\nAlso asked as:\n"
        + "\n".join(f"- {q}" for q in art.get("question_paraphrases", []))
        + f"\n\n{art.get('answer_markdown', '')}\n\n## Limits and exceptions\n"
        + "\n".join(f"- {c}" for c in art.get("constraints", []))
        + "\n\n## When we connect you with a specialist\n"
        + "\n".join(f"- {c}" for c in art.get("escalate_when", []))
        + "\n\n_Global Retail Hub is a simulated retailer; policy details are illustrative._\n"
    )


def synthesize_article(th, threads, tok, project, gemini_fn=gemini, max_retries=1):
    """Write ONE article with policy fact constraints and automatic lint retry.

    gemini_fn: callable(tok, project, prompt, schema) -> (art_dict, usage_dict).
    Returns (th, art, body, usage_dict, n_examples).
    """
    doc_id = doc_id_for_theme(th)
    ex = [
        threads[t]
        for t in th.get("thread_ids", [])
        if t in threads and any(x.get("role") == "agent" for x in threads[t].get("turns", []))
    ][:15]
    examples = "\n\n---\n".join(thread_text(t) for t in ex)

    base_prompt = (
        f"You write help-center FAQ articles for Global Retail Hub, a simulated online retailer.\n{SIMULATED_POLICY}\n\n"
        f"CRITICAL POLICY NUMBERS INSTRUCTION:\n"
        f"- Use ONLY the numbers, timeframes, and dollar amounts explicitly stated in the policy above.\n"
        f"- For example: '24 hours', '1 business day', '2 business days', '3-5 business days', '4-6 business days', '14 days', '30 days', '90 days', '$35'.\n"
        f"- Do NOT invent ANY other numbers, timeframes (such as 48 hours, 7 days, 10 days, 72 hours, etc.), or percentages.\n\n"
        f"Theme: {th['title']} — {th['description']} (intent family: {th['intent_family']}).\n"
        f"Below are real, PII-redacted customer messages and support-agent replies on this theme (from another retailer; "
        f"use them for the customer's language and the troubleshooting steps agents used, NOT for brand names or policy numbers).\n\n"
        f"{examples}\n\nWrite ONE article:\n- title: a customer question, e.g. 'What should I do if my package is marked delivered but I can't find it?'\n"
        f"- question_paraphrases: 6-10 ways customers actually ask this, informal, varied\n"
        f"- answer_markdown: 120-300 words, direct answer first, then numbered steps; use only the policy facts above; "
        f"never ask for passwords or full card numbers; brand is 'Global Retail Hub', membership is 'Hub Plus'; do not invent numbers or timeframes\n"
        f"- constraints: exceptions and limits (from the policy)\n- escalate_when: 2-4 situations that need a human agent"
    )

    prompt = base_prompt
    total_usage = collections.Counter()
    final_art = None
    final_body = None

    for attempt in range(max_retries + 1):
        art, u = gemini_fn(tok, project, prompt, ARTICLE_SCHEMA)
        for k_, v in u.items():
            if isinstance(v, int):
                total_usage[k_] += v
        body = build_article_body(art)
        lint_fn = get_lint_fn()
        bad = lint_fn({doc_id: body})
        if not bad or attempt >= max_retries:
            final_art, final_body = art, body
            break

        # Lint failed: retry once with targeted feedback
        violations = bad.get(doc_id, [])
        prompt = (
            f"{base_prompt}\n\n"
            f"PREVIOUS ATTEMPT FAILED POLICY LINT:\n"
            f"The previous attempt contained unauthorized policy numbers: {violations}.\n"
            f"You MUST NOT use these numbers. Use ONLY the numbers from the canonical policy.\n"
        )

    return th, final_art, final_body, dict(total_usage), len(ex)


def build_corpus(
    threads_path,
    out_dir,
    project,
    labels_path=None,
    themes_path=None,
    reuse_from=None,
    articles_from=None,
    min_theme_size=6,
    workers=8,
    gcs_prefix="gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v1",
    effective_from=None,
    tok=None,
    gemini_fn=gemini,
):
    """Build FAQ corpus: load or cluster themes, synthesize/reuse articles, and produce import jsonl."""
    if effective_from is None:
        effective_from = datetime.date.today().isoformat()
    if tok is None:
        tok = Tok()

    out = pathlib.Path(out_dir)
    (out / "docs").mkdir(parents=True, exist_ok=True)
    threads = {str(json.loads(l)["thread_id"]): json.loads(l) for l in open(threads_path)}
    usage = collections.Counter()

    # 1) Acquire themes: either from --themes or by clustering --labels
    if themes_path:
        themes = json.load(open(themes_path))
    else:
        if not labels_path:
            raise ValueError("--labels is required when --themes is not provided")
        msgs = collections.defaultdict(list)  # intent -> [(thread_id, topic)]
        for l in open(labels_path):
            r = json.loads(l)
            topics = [v.get("topic") for v in r.get("votes", []) if v.get("topic")]
            if not topics:
                continue
            probs = r.get("gold", {}).get("intent", {}).get("probabilities", {})
            if not probs:
                continue
            intent = max(probs, key=probs.get)
            msgs[intent].append((str(r["thread_id"]), topics[0].strip().lower()))

        themes = []
        for intent in INTENTS:
            cnt = collections.Counter(t for _, t in msgs[intent])
            topic_list = "\n".join(f"{n}\t{t}" for t, n in cnt.most_common(700))
            k = "12-30" if intent == "other" else "6-18"
            res, u = gemini_fn(
                tok,
                project,
                f"These are short issue topics (count<TAB>topic) from customer-support messages to an online retailer, "
                f"all labeled intent='{intent}'. Group them into {k} FAQ themes, each answerable by ONE help-center article. "
                f"Every topic must appear in exactly one theme; copy topic strings exactly. theme_id: short snake_case.\n\n{topic_list}",
                THEME_SCHEMA,
            )
            for k_, v in u.items():
                if isinstance(v, int):
                    usage[k_] += v
            t2theme = {}
            for th in res["themes"]:
                for t in th["topics"]:
                    t2theme[t.strip().lower()] = th["theme_id"]
            for th in res["themes"]:
                members = [tid for tid, t in msgs[intent] if t2theme.get(t) == th["theme_id"]]
                themes.append({**th, "intent_family": intent, "thread_ids": members})
            print(
                f"{intent}: {len(res['themes'])} themes from {len(cnt)} topics / {len(msgs[intent])} messages",
                flush=True,
            )
        json.dump(themes, open(out / "themes.json", "w"), indent=1)

    # 2) One article per sufficiently large theme
    todo = [t for t in themes if len(t.get("thread_ids", [])) >= min_theme_size]
    reuse_dir = pathlib.Path(reuse_from) if reuse_from else None
    articles_dir = pathlib.Path(articles_from) if articles_from else None

    to_synthesize = []
    articles = []

    for th in todo:
        doc_id = doc_id_for_theme(th)
        handled = False

        # 2a. Pre-authored articles from --articles-from (newly authored or repaired articles)
        if articles_dir is not None:
            art_file = articles_dir / f"{doc_id}.json"
            if art_file.exists():
                art = json.load(open(art_file))
                body = build_article_body(art)
                lint_fn = get_lint_fn()
                bad = lint_fn({doc_id: body})
                if bad:
                    raise ValueError(f"Authored article '{doc_id}' failed policy lint: {bad[doc_id]}")
                dest_file = out / "docs" / f"{doc_id}.txt"
                dest_file.write_text(body, encoding="utf-8")
                ex = [
                    threads[t]
                    for t in th.get("thread_ids", [])
                    if t in threads and any(x.get("role") == "agent" for x in threads[t].get("turns", []))
                ][:15]
                articles.append({
                    "id": doc_id,
                    "structData": {
                        "article_id": doc_id,
                        "title": art["title"],
                        "intent_family": th["intent_family"],
                        "theme": th["title"],
                        "version": 1,
                        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "status": "draft",
                        "effective_from": effective_from,
                        "effective_to": "",
                        "supersedes": "",
                        "policy_version": POLICY_VERSION,
                        "reviewed_by": "",
                        "reviewed_at": "",
                        "authored_by": f"jetski-agent:{model_id('agent_authorship_tag')}",
                        "n_source_messages": len(th.get("thread_ids", [])),
                        "n_example_threads": len(ex),
                        "simulated_policy": True,
                    },
                    "content": {"mimeType": "text/plain", "uri": f"{gcs_prefix}/docs/{doc_id}.txt"},
                })
                handled = True

        # 2b. Reused unchanged articles from --reuse-from
        if not handled and reuse_dir is not None:
            candidate_file = reuse_dir / f"{doc_id}.txt"
            if candidate_file.exists():
                candidate_body = candidate_file.read_text(encoding="utf-8")
                lint_fn = get_lint_fn()
                if not lint_fn({doc_id: candidate_body}):
                    dest_file = out / "docs" / f"{doc_id}.txt"
                    dest_file.write_bytes(candidate_file.read_bytes())
                    title = candidate_body.splitlines()[0].lstrip("# ").strip()
                    ex = [
                        threads[t]
                        for t in th.get("thread_ids", [])
                        if t in threads and any(x.get("role") == "agent" for x in threads[t].get("turns", []))
                    ][:15]
                    articles.append({
                        "id": doc_id,
                        "structData": {
                            "article_id": doc_id,
                            "title": title,
                            "intent_family": th["intent_family"],
                            "theme": th["title"],
                            "version": 1,
                            "content_sha256": hashlib.sha256(candidate_body.encode()).hexdigest(),
                            "status": "draft",
                            "effective_from": effective_from,
                            "effective_to": "",
                            "supersedes": "",
                            "policy_version": POLICY_VERSION,
                            "reviewed_by": "",
                            "reviewed_at": "",
                            "n_source_messages": len(th.get("thread_ids", [])),
                            "n_example_threads": len(ex),
                            "simulated_policy": True,
                        },
                        "content": {"mimeType": "text/plain", "uri": f"{gcs_prefix}/docs/{doc_id}.txt"},
                    })
                    handled = True

        if not handled:
            to_synthesize.append(th)

    def write_worker(th):
        return synthesize_article(th, threads, tok, project, gemini_fn=gemini_fn)

    if to_synthesize:
        actual_workers = min(workers, len(to_synthesize))
        with cf.ThreadPoolExecutor(actual_workers) as pool:
            for th, art, body, u, n_ex in pool.map(write_worker, to_synthesize):
                for k_, v in u.items():
                    if isinstance(v, int):
                        usage[k_] += v
                doc_id = doc_id_for_theme(th)
                (out / "docs" / f"{doc_id}.txt").write_text(body, encoding="utf-8")
                articles.append({
                    "id": doc_id,
                    "structData": {
                        "article_id": doc_id,
                        "title": art["title"],
                        "intent_family": th["intent_family"],
                        "theme": th["title"],
                        "version": 1,
                        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "status": "draft",
                        "effective_from": effective_from,
                        "effective_to": "",
                        "supersedes": "",
                        "policy_version": POLICY_VERSION,
                        "reviewed_by": "",
                        "reviewed_at": "",
                        "n_source_messages": len(th.get("thread_ids", [])),
                        "n_example_threads": n_ex,
                        "simulated_policy": True,
                    },
                    "content": {"mimeType": "text/plain", "uri": f"{gcs_prefix}/docs/{doc_id}.txt"},
                })

    articles.sort(key=lambda x: x["id"])
    with open(out / "faq_articles_import.jsonl", "w") as fh:
        for x in articles:
            fh.write(json.dumps(x) + "\n")

    print(
        f"articles: {len(articles)} (themes {len(themes)}, reused {len(todo) - len(to_synthesize)}, "
        f"synthesized {len(to_synthesize)}); usage {dict(usage)}"
    )
    return articles, dict(usage)


def main():
    ap = argparse.ArgumentParser(description="Synthesize the citable faq-articles corpus for Agent Search")
    ap.add_argument("--labels", default=None, help="Path to labels.jsonl (required if --themes not given)")
    ap.add_argument("--threads", required=True, help="Path to threads.jsonl")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--project", required=True, help="GCP project ID")
    ap.add_argument("--themes", default=None, help="Path to existing themes JSON (bypasses clustering)")
    ap.add_argument("--reuse-from", default=None, help="Path to existing v1 docs/ directory to copy unchanged articles byte-identical")
    ap.add_argument("--articles-from", default=None, help="Path to directory containing pre-authored article JSON files (<doc_id>.json)")
    ap.add_argument("--min-theme-size", type=int, default=6)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--gcs-prefix", default="gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v1")
    ap.add_argument("--effective-from", default=datetime.date.today().isoformat())
    a = ap.parse_args()

    if not a.themes and not a.labels:
        ap.error("--labels is required when --themes is not provided")

    tok = Tok()
    build_corpus(
        threads_path=a.threads,
        out_dir=a.out,
        project=a.project,
        labels_path=a.labels,
        themes_path=a.themes,
        reuse_from=a.reuse_from,
        articles_from=a.articles_from,
        min_theme_size=a.min_theme_size,
        workers=a.workers,
        gcs_prefix=a.gcs_prefix,
        effective_from=a.effective_from,
        tok=tok,
    )


if __name__ == "__main__":
    main()
