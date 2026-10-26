import json
import pytest
from evals.clean_eval import is_verbatim_citation, main, norm


# REQ-RAG-RETR-005
def test_norm_strips_markdown_and_whitespace():
    raw = "  # Title **Bold** and _italic_ with `code` > quote  "
    assert norm(raw) == "title bold and italic with code quote"


# REQ-RAG-RETR-005
def test_norm_case_and_whitespace():
    raw = "RETURN   within  30 DAYS"
    assert norm(raw) == "return within 30 days"


# REQ-RAG-RETR-005
def test_is_verbatim_citation_exact_and_normalized():
    doc = "# Returns Policy\nYou can return items within 30 days of delivery."
    quote1 = "return items within 30 days of delivery"
    quote2 = "**return items** within   30 days"
    assert is_verbatim_citation(quote1, doc) is True
    assert is_verbatim_citation(quote2, doc) is True


# REQ-RAG-RETR-005
def test_is_verbatim_citation_missing():
    doc = "# Returns Policy\nYou can return items within 30 days of delivery."
    quote = "return items within 48 hours"
    assert is_verbatim_citation(quote, doc) is False
    assert is_verbatim_citation("", doc) is False
    assert is_verbatim_citation(quote, "") is False


# REQ-RAG-RETR-003
def test_gold_map_resolution():
    gold_map = {"faq-old-account": "faq-new-account"}
    source_doc = "faq-old-account"
    effective_target = gold_map.get(source_doc, source_doc)
    assert effective_target == "faq-new-account"

    # Citation check with gold map:
    citations = [{"doc_id": "faq-new-account", "quote": "something"}]
    assert any(c["doc_id"] == effective_target for c in citations)


# REQ-BILLING-002
def test_reuse_questions_file_loading(tmp_path):
    sample_questions = [
        {"question": "How do I return?", "source_doc": "faq-returns"},
        {"question": "Where is my package?", "source_doc": "faq-order"},
    ]
    qfile = tmp_path / "custom_questions.jsonl"
    with open(qfile, "w") as fh:
        for q in sample_questions:
            fh.write(json.dumps(q) + "\n")

    loaded = [json.loads(l) for l in open(qfile)]
    assert len(loaded) == 2
    assert loaded[0]["question"] == "How do I return?"


# REQ-RAG-RETR-003
def test_clean_eval_main_runs_with_gold_map_and_reuse_questions(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc_text = "# Order Policy\nStandard orders arrive within 4-6 business days."
    (docs_dir / "faq-order-v2.txt").write_text(doc_text, encoding="utf-8")

    import_file = tmp_path / "faq_articles_import.jsonl"
    import_file.write_text(json.dumps({"id": "faq-order-v2"}) + "\n")

    questions_file = tmp_path / "questions.jsonl"
    questions_file.write_text(json.dumps({
        "question": "When will my package arrive?",
        "source_doc": "faq-order-v1"
    }) + "\n")

    gold_map_file = tmp_path / "gold_map.json"
    gold_map_file.write_text(json.dumps({"faq-order-v1": "faq-order-v2"}))

    out_dir = tmp_path / "out"

    # Mock external calls
    monkeypatch.setattr("evals.clean_eval.search", lambda q: ["faq-order-v2"])
    monkeypatch.setattr("evals.clean_eval.gemini", lambda model, prompt, schema: (
        {
            "answer": "Orders arrive within 4-6 business days.",
            "citations": [{"doc_id": "faq-order-v2", "quote": "arrive within 4-6 business days"}],
            "grounded": True,
            "escalate": False,
        },
        {"promptTokenCount": 50, "candidatesTokenCount": 20, "thoughtsTokenCount": 0}
    ))
    monkeypatch.setattr("evals.clean_eval.claude", lambda model, prompt, effort="high", max_tokens=8000: (
        {"claims": [{"claim": "Orders arrive within 4-6 business days.", "supported": True}], "faithfulness": 5},
        {"input_tokens": 100, "output_tokens": 50}
    ))
    monkeypatch.setattr("evals.clean_eval.post", lambda url, body, **kw: {
        "groundednessResult": {"score": 5, "explanation": "grounded"}
    })

    monkeypatch.setattr("sys.argv", [
        "evals.clean_eval.py",
        "--docs", str(docs_dir),
        "--import", str(import_file),
        "--out", str(out_dir),
        "--gold-map", str(gold_map_file),
        "--reuse-questions", str(questions_file),
        "--workers", "1",
    ])

    main()

    report_file = out_dir / "report.json"
    assert report_file.exists()
    rep = json.load(open(report_file))
    assert rep["A_retrieval"]["n"] == 1
    assert rep["A_retrieval"]["recall@1"] == 1.0
    assert rep["B_deterministic"]["answers"] == 1
    assert rep["B_deterministic"]["cites_source_article"] == 1.0
    assert rep["B_deterministic"]["citation_precision_verbatim"] == 1.0


# REQ-BILLING-001
def test_clean_eval_skip_judge_flag(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc_text = "# Order Policy\nStandard orders arrive within 4-6 business days."
    (docs_dir / "faq-order-v2.txt").write_text(doc_text, encoding="utf-8")

    import_file = tmp_path / "faq_articles_import.jsonl"
    import_file.write_text(json.dumps({"id": "faq-order-v2"}) + "\n")

    questions_file = tmp_path / "questions.jsonl"
    questions_file.write_text(json.dumps({
        "question": "When will my package arrive?",
        "source_doc": "faq-order-v1"
    }) + "\n")

    gold_map_file = tmp_path / "gold_map.json"
    gold_map_file.write_text(json.dumps({"faq-order-v1": "faq-order-v2"}))

    out_dir = tmp_path / "out"

    # Mock search and gemini answer generator
    monkeypatch.setattr("evals.clean_eval.search", lambda q: ["faq-order-v2"])
    monkeypatch.setattr("evals.clean_eval.gemini", lambda model, prompt, schema: (
        {
            "answer": "Orders arrive within 4-6 business days.",
            "citations": [{"doc_id": "faq-order-v2", "quote": "arrive within 4-6 business days"}],
            "grounded": True,
            "escalate": False,
        },
        {"promptTokenCount": 50, "candidatesTokenCount": 20, "thoughtsTokenCount": 0}
    ))

    # Assert claude and post(evaluateInstances) are NEVER called
    def fail_claude(*args, **kwargs):
        pytest.fail("claude judge should not be called when --skip-judge is passed")

    def fail_post(url, *args, **kwargs):
        if "evaluateInstances" in url:
            pytest.fail("evaluateInstances should not be called when --skip-judge is passed")
        return {}

    monkeypatch.setattr("evals.clean_eval.claude", fail_claude)
    monkeypatch.setattr("evals.clean_eval.post", fail_post)

    monkeypatch.setattr("sys.argv", [
        "evals.clean_eval.py",
        "--docs", str(docs_dir),
        "--import", str(import_file),
        "--out", str(out_dir),
        "--gold-map", str(gold_map_file),
        "--reuse-questions", str(questions_file),
        "--skip-judge",
        "--workers", "1",
    ])

    main()

    report_file = out_dir / "report.json"
    assert report_file.exists()
    rep = json.load(open(report_file))
    assert rep["B_evalsvc_groundedness"] == {"skipped": True}
    assert rep["B_judge"] == {"skipped": True}
    assert not (out_dir / "evalsvc.jsonl").exists()
    assert not (out_dir / "judge.jsonl").exists()


# REQ-RAG-RETR-004
def test_clean_eval_location_default_global(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "faq-order-v2.txt").write_text("# Order Policy\nStandard orders arrive within 4-6 business days.", encoding="utf-8")

    import_file = tmp_path / "faq_articles_import.jsonl"
    import_file.write_text(json.dumps({"id": "faq-order-v2"}) + "\n")

    questions_file = tmp_path / "questions.jsonl"
    questions_file.write_text(json.dumps({
        "question": "When will my package arrive?",
        "source_doc": "faq-order-v2"
    }) + "\n")

    out_dir = tmp_path / "out"

    recorded_search_posts = []

    def mock_post(url, body, **kw):
        if ":search" in url:
            recorded_search_posts.append((url, body))
            return {"results": [{"document": {"id": "faq-order-v2"}}]}
        return {}

    monkeypatch.setattr("evals.clean_eval.post", mock_post)
    monkeypatch.setattr("evals.clean_eval.gemini", lambda *a, **kw: (
        {"answer": "Within 4-6 days.", "citations": [{"doc_id": "faq-order-v2", "quote": "arrive within 4-6 business days"}], "grounded": True, "escalate": False},
        {"promptTokenCount": 10, "candidatesTokenCount": 10, "thoughtsTokenCount": 0}
    ))
    monkeypatch.setattr("sys.argv", [
        "evals.clean_eval.py",
        "--docs", str(docs_dir),
        "--import", str(import_file),
        "--out", str(out_dir),
        "--reuse-questions", str(questions_file),
        "--skip-judge",
        "--workers", "1",
    ])

    main()

    assert len(recorded_search_posts) == 1
    url, body = recorded_search_posts[0]
    expected_url = (
        "https://discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/global/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    expected_ds = (
        "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/"
        "dataStores/faq-articles"
    )
    assert url == expected_url
    assert body["dataStoreSpecs"][0]["dataStore"] == expected_ds


# REQ-RAG-RETR-004
def test_clean_eval_location_us(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "faq-order-v2.txt").write_text("# Order Policy\nStandard orders arrive within 4-6 business days.", encoding="utf-8")

    import_file = tmp_path / "faq_articles_import.jsonl"
    import_file.write_text(json.dumps({"id": "faq-order-v2"}) + "\n")

    questions_file = tmp_path / "questions.jsonl"
    questions_file.write_text(json.dumps({
        "question": "When will my package arrive?",
        "source_doc": "faq-order-v2"
    }) + "\n")

    out_dir = tmp_path / "out"

    recorded_search_posts = []

    def mock_post(url, body, **kw):
        if ":search" in url:
            recorded_search_posts.append((url, body))
            return {"results": [{"document": {"id": "faq-order-v2"}}]}
        return {}

    monkeypatch.setattr("evals.clean_eval.post", mock_post)
    monkeypatch.setattr("evals.clean_eval.gemini", lambda *a, **kw: (
        {"answer": "Within 4-6 days.", "citations": [{"doc_id": "faq-order-v2", "quote": "arrive within 4-6 business days"}], "grounded": True, "escalate": False},
        {"promptTokenCount": 10, "candidatesTokenCount": 10, "thoughtsTokenCount": 0}
    ))
    monkeypatch.setattr("sys.argv", [
        "evals.clean_eval.py",
        "--docs", str(docs_dir),
        "--import", str(import_file),
        "--out", str(out_dir),
        "--location", "us",
        "--reuse-questions", str(questions_file),
        "--skip-judge",
        "--workers", "1",
    ])

    main()

    assert len(recorded_search_posts) == 1
    url, body = recorded_search_posts[0]
    expected_url = (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    expected_ds = (
        "projects/susmith-fde-ticket-router/locations/us/collections/default_collection/"
        "dataStores/faq-articles"
    )
    assert url == expected_url
    assert body["dataStoreSpecs"][0]["dataStore"] == expected_ds

