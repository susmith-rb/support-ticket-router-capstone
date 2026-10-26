"""tests/rag/test_build_faq_corpus.py — unit tests for FAQ corpus builder (reuse-from, lint retry, offline)."""

import hashlib
import json

from corpus.build_faq_corpus import (
    POLICY_VERSION,
    build_article_body,
    build_corpus,
    doc_id_for_theme,
    synthesize_article,
)
from corpus.publish_faq import lint


def _make_dummy_threads_file(tmp_path):
    """Create a temporary threads.jsonl with a few threads."""
    threads_file = tmp_path / "threads.jsonl"
    lines = [
        {
            "thread_id": "1001",
            "turns": [
                {"role": "customer", "text": "My package hasn't arrived yet!"},
                {"role": "agent", "text": "I can check your tracking number."},
            ],
        },
        {
            "thread_id": "1002",
            "turns": [
                {"role": "customer", "text": "I want to return this damaged item."},
                {"role": "agent", "text": "You can return it within 30 days for a free replacement."},
            ],
        },
    ]
    with open(threads_file, "w") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")
    return threads_file


# REQ-RAG-CORPUS-001
def test_build_article_body_formatting():
    art = {
        "title": "What should I do if my order is late?",
        "question_paraphrases": ["My order is late", "Where is my delivery?"],
        "answer_markdown": "A package is considered late 1 business day after the latest estimate.",
        "constraints": ["Standard shipping is 4-6 business days."],
        "escalate_when": ["The order is more than 3 business days late."],
    }
    body = build_article_body(art)
    assert body.startswith("# What should I do if my order is late?\n\nAlso asked as:\n- My order is late\n- Where is my delivery?\n\n")
    assert "A package is considered late 1 business day after the latest estimate." in body
    assert "## Limits and exceptions\n- Standard shipping is 4-6 business days." in body
    assert "## When we connect you with a specialist\n- The order is more than 3 business days late." in body
    assert "_Global Retail Hub is a simulated retailer; policy details are illustrative._" in body


# REQ-RAG-CORPUS-002
def test_lint_retry_path_with_fake_llm():
    """Verify that when an LLM produces an invented policy fact (e.g. 48 hours),

    synthesize_article catches it via publish_faq.lint, retries once with feedback,
    and returns the clean version.
    """
    call_log = []

    def fake_gemini(tok, project, prompt, schema):
        call_log.append(prompt)
        if len(call_log) == 1:
            # First attempt: invent an unauthorized timeframe ("48 hours")
            art = {
                "title": "How do I unlock my account?",
                "question_paraphrases": ["Account locked", "Cannot sign in"],
                "answer_markdown": "If your account is locked, our team will respond within 48 hours to help you.",
                "constraints": ["Support never asks for passwords."],
                "escalate_when": ["Suspected unauthorized access."],
            }
        else:
            # Second attempt (retry): use canonical policy fact ("24 hours")
            art = {
                "title": "How do I unlock my account?",
                "question_paraphrases": ["Account locked", "Cannot sign in"],
                "answer_markdown": "If your account is locked, we will investigate and reply within 24 hours.",
                "constraints": ["Support never asks for passwords."],
                "escalate_when": ["Suspected unauthorized access."],
            }
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        return art, usage

    theme = {
        "theme_id": "account_locked_blocked",
        "title": "Account Locked, Blocked, or Suspended",
        "description": "Account security and unlock requests",
        "intent_family": "account_payment",
        "thread_ids": ["1001"],
    }
    threads = {"1001": {"thread_id": "1001", "turns": [{"role": "agent", "text": "We will help."}]}}

    th, final_art, body, usage, n_ex = synthesize_article(
        theme, threads, tok=lambda: "fake-tok", project="fake-proj", gemini_fn=fake_gemini, max_retries=1
    )

    # 1. Exactly 2 attempts were made
    assert len(call_log) == 2

    # 2. Second prompt contained feedback about the lint violation
    assert "PREVIOUS ATTEMPT FAILED POLICY LINT:" in call_log[1]
    assert "48 hour" in call_log[1]

    # 3. Final article body has clean text and passes lint
    doc_id = doc_id_for_theme(theme)
    assert "within 24 hours" in body
    assert "within 48 hours" not in body
    bad = lint({doc_id: body})
    assert len(bad) == 0


# REQ-RAG-CORPUS-003
def test_reuse_from_copies_unchanged_articles_byte_identical(tmp_path):
    """Verify that --reuse-from copies existing clean articles byte-identical without calling LLM."""
    v1_docs_dir = tmp_path / "v1_docs"
    v1_docs_dir.mkdir()

    # Create an unchanged clean article with unique content and formatting
    clean_doc_id = "faq-shipping-policy-shipping-costs-and-fees"
    clean_content = (
        "# What are the shipping costs and fees?\n\n"
        "Also asked as:\n"
        "- How much is shipping?\n"
        "- Is delivery free?\n\n"
        "Standard shipping is 4-6 business days and is free on orders over $35.\n"
        "For Hub Plus members, 2 business days shipping is free on eligible items.\n\n"
        "## Limits and exceptions\n"
        "- Free shipping over $35.\n\n"
        "## When we connect you with a specialist\n"
        "- Special shipping quote needed.\n\n"
        "_Global Retail Hub is a simulated retailer; policy details are illustrative._\n"
    )
    (v1_docs_dir / f"{clean_doc_id}.txt").write_bytes(clean_content.encode("utf-8"))

    # Also create a doc in v1 that fails lint (e.g. contains 48 hours)
    bad_doc_id = "faq-account-payment-account-locked-blocked"
    bad_content = (
        "# How do I unlock my account?\n\n"
        "Also asked as:\n"
        "- Account locked\n\n"
        "We will respond within 48 hours.\n\n"
        "## Limits and exceptions\n"
        "- Support never asks for passwords.\n\n"
        "## When we connect you with a specialist\n"
        "- Immediate help.\n\n"
        "_Global Retail Hub is a simulated retailer; policy details are illustrative._\n"
    )
    (v1_docs_dir / f"{bad_doc_id}.txt").write_bytes(bad_content.encode("utf-8"))

    # Setup themes: one clean (reusable), one bad (must NOT be reused, must be synthesized),
    # and one brand new (must be synthesized)
    new_doc_id = "faq-order-status-late-or-not-arrived-order"
    themes = [
        {
            "theme_id": "shipping_costs_and_fees",
            "title": "Shipping Costs & Fees",
            "description": "Shipping costs",
            "intent_family": "shipping_policy",
            "thread_ids": ["1001", "1002", "1003", "1004", "1005", "1006"],
        },
        {
            "theme_id": "account_locked_blocked",
            "title": "Account Locked",
            "description": "Lockout issues",
            "intent_family": "account_payment",
            "thread_ids": ["1001", "1002", "1003", "1004", "1005", "1006"],
        },
        {
            "theme_id": "late_or_not_arrived_order",
            "title": "Late or Not-Arrived Orders",
            "description": "Late orders",
            "intent_family": "order_status",
            "thread_ids": ["1001", "1002", "1003", "1004", "1005", "1006"],
        },
    ]

    themes_file = tmp_path / "themes_v2.json"
    themes_file.write_text(json.dumps(themes))

    threads_file = _make_dummy_threads_file(tmp_path)
    out_dir = tmp_path / "out_v2"

    synthesized_doc_ids = []

    def fake_gemini(tok, project, prompt, schema):
        # Extract title from prompt
        re_title = None
        for line in prompt.splitlines():
            if line.startswith("Theme: "):
                re_title = line.split("—")[0].replace("Theme: ", "").strip()
                break
        title = re_title or "Generated FAQ"
        # Determine which theme is being synthesized
        if "Late" in title:
            synthesized_doc_ids.append(new_doc_id)
        elif "Account Locked" in title:
            synthesized_doc_ids.append(bad_doc_id)
        else:
            synthesized_doc_ids.append("other")

        art = {
            "title": f"FAQ: {title}",
            "question_paraphrases": ["Question 1", "Question 2"],
            "answer_markdown": "A package is late 1 business day after the latest estimate. Contact support within 24 hours.",
            "constraints": ["Policy applies."],
            "escalate_when": ["Customer requests specialist."],
        }
        return art, {"prompt_tokens": 50, "completion_tokens": 20}

    articles, usage = build_corpus(
        threads_path=str(threads_file),
        out_dir=str(out_dir),
        project="test-project",
        themes_path=str(themes_file),
        reuse_from=str(v1_docs_dir),
        min_theme_size=6,
        workers=1,
        tok=lambda: "token",
        gemini_fn=fake_gemini,
    )

    # 1. Verify clean article was copied BYTE-IDENTICAL
    dest_file = out_dir / "docs" / f"{clean_doc_id}.txt"
    assert dest_file.exists()
    assert dest_file.read_bytes() == (v1_docs_dir / f"{clean_doc_id}.txt").read_bytes()

    # 2. Verify LLM was NOT called for clean_doc_id
    assert clean_doc_id not in synthesized_doc_ids

    # 3. Verify bad article (failed lint in v1) was NOT reused, but synthesized fresh
    assert bad_doc_id in synthesized_doc_ids
    bad_dest = out_dir / "docs" / f"{bad_doc_id}.txt"
    assert bad_dest.exists()
    assert bad_dest.read_bytes() != (v1_docs_dir / f"{bad_doc_id}.txt").read_bytes()
    # And passes lint now!
    assert len(lint({bad_doc_id: bad_dest.read_text()})) == 0

    # 4. Verify new article was synthesized
    assert new_doc_id in synthesized_doc_ids
    assert (out_dir / "docs" / f"{new_doc_id}.txt").exists()

    # 5. Verify import jsonl was created with correct metadata
    import_file = out_dir / "faq_articles_import.jsonl"
    assert import_file.exists()
    records = {json.loads(l)["id"]: json.loads(l) for l in import_file.read_text().splitlines()}
    assert clean_doc_id in records
    clean_sd = records[clean_doc_id]["structData"]
    assert clean_sd["article_id"] == clean_doc_id
    assert clean_sd["version"] == 1
    assert clean_sd["status"] == "draft"
    assert clean_sd["content_sha256"] == hashlib.sha256(clean_content.encode("utf-8")).hexdigest()


# REQ-RAG-CORPUS-004
def test_articles_from_mode_renders_docs_and_sets_provenance(tmp_path):
    """Verify that --articles-from renders pre-authored JSON articles and sets authored_by provenance."""
    articles_dir = tmp_path / "authored_articles"
    articles_dir.mkdir()

    doc_id = "faq-order-status-late-or-not-arrived-order"
    article_data = {
        "title": "What should I do if my order is late or has not arrived?",
        "question_paraphrases": ["My order is late", "Where is my package?"],
        "answer_markdown": "A package is late 1 business day after the latest estimate. Contact support within 24 hours.",
        "constraints": ["Standard shipping is 4-6 business days."],
        "escalate_when": ["Customer requests urgent specialist help."],
    }
    (articles_dir / f"{doc_id}.json").write_text(json.dumps(article_data))

    themes = [
        {
            "theme_id": "late_or_not_arrived_order",
            "title": "Late or Not-Arrived Orders",
            "description": "Late orders",
            "intent_family": "order_status",
            "thread_ids": ["1001", "1002", "1003", "1004", "1005", "1006"],
        }
    ]
    themes_file = tmp_path / "themes.json"
    themes_file.write_text(json.dumps(themes))

    threads_file = _make_dummy_threads_file(tmp_path)
    out_dir = tmp_path / "out"

    articles, usage = build_corpus(
        threads_path=str(threads_file),
        out_dir=str(out_dir),
        project="test-project",
        themes_path=str(themes_file),
        articles_from=str(articles_dir),
        min_theme_size=6,
        workers=1,
        tok=lambda: "token",
    )

    # 1. Output doc rendered
    dest_file = out_dir / "docs" / f"{doc_id}.txt"
    assert dest_file.exists()
    assert dest_file.read_text().startswith("# What should I do if my order is late or has not arrived?")
    assert lint({doc_id: dest_file.read_text()}) == {}

    # 2. Output manifest has provenance
    import_file = out_dir / "faq_articles_import.jsonl"
    record = json.loads(import_file.read_text().strip())
    assert record["id"] == doc_id
    assert record["structData"]["authored_by"] == "jetski-agent:gemini-3.8-flash-high"
    assert record["structData"]["policy_version"] == POLICY_VERSION

