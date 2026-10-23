import json
import pytest
from retrieval_eval import clean, compute_rank, compute_recall_at_k, compute_mrr, main


def test_clean_strips_mentions_and_tokens():
    raw = "@AmazonHelp where is my order [ORDER] sent to [EMAIL]?"
    cleaned = clean(raw)
    assert "@AmazonHelp" not in cleaned
    assert "[ORDER]" not in cleaned
    assert "[EMAIL]" not in cleaned
    assert "where is my order" in cleaned


def test_clean_handles_hashtags_and_whitespace():
    raw = "package #late    arrived   #damaged"
    cleaned = clean(raw)
    assert "#" not in cleaned
    assert "  " not in cleaned
    assert cleaned == "package late arrived damaged"


def test_clean_fallback_behavior():
    # If cleaned string is whitespace/empty, fallback 'or q' returns the original string q
    assert clean("   ") == "   "
    assert clean("@user") == "@user"
    assert clean("[ORDER]") == "[ORDER]"
    assert clean("@AmazonHelp [URL]") == "@AmazonHelp [URL]"


def test_compute_rank_single_target():
    ids = ["doc-a", "doc-b", "doc-c"]
    assert compute_rank("doc-a", ids) == 1
    assert compute_rank("doc-b", ids) == 2
    assert compute_rank("doc-c", ids) == 3
    assert compute_rank("doc-missing", ids) is None


def test_compute_rank_multiple_targets():
    ids = ["doc-x", "doc-b", "doc-a"]
    # target is a list or tuple of equivalent valid targets (e.g. from gold-map)
    assert compute_rank(["doc-a", "doc-b"], ids) == 2  # doc-b is at rank 2, doc-a at rank 3
    assert compute_rank(["doc-none", "doc-x"], ids) == 1
    assert compute_rank(["doc-1", "doc-2"], ids) is None


def test_compute_recall_at_k():
    ranks = [1, 2, 5, 6, None, 10]
    assert compute_recall_at_k(ranks, k=1) == pytest.approx(1 / 6)
    assert compute_recall_at_k(ranks, k=3) == pytest.approx(2 / 6)
    assert compute_recall_at_k(ranks, k=5) == pytest.approx(3 / 6)
    assert compute_recall_at_k(ranks, k=10) == pytest.approx(5 / 6)
    assert compute_recall_at_k([], k=5) == 0.0


def test_compute_mrr():
    # ranks: [1, 2, None, 4] -> (1/1 + 1/2 + 0 + 1/4) / 4 = 1.75 / 4 = 0.4375
    ranks = [1, 2, None, 4]
    assert compute_mrr(ranks) == pytest.approx(1.75 / 4)
    assert compute_mrr([]) == 0.0
    assert compute_mrr([None, None]) == 0.0


def test_gold_map_mapping():
    # Verify gold_map remapping logic
    gold_map = {"faq-old-order": "faq-new-order-merged"}
    old_target = "faq-old-order"
    effective_target = gold_map.get(old_target, old_target)
    assert effective_target == "faq-new-order-merged"

    ids = ["faq-other", "faq-new-order-merged"]
    # Without gold map:
    assert compute_rank(old_target, ids) is None
    # With gold map:
    assert compute_rank(effective_target, ids) == 2


def test_retrieval_eval_main_runs_with_and_without_gold_map(tmp_path, monkeypatch):
    threads_file = tmp_path / "threads.jsonl"
    themes_file = tmp_path / "themes.json"
    import_file = tmp_path / "faq_articles_import.jsonl"
    out_file1 = tmp_path / "out1.json"
    out_file2 = tmp_path / "out2.json"
    gold_map_file = tmp_path / "gold_map.json"

    # Write test fixtures
    threads_file.write_text(json.dumps({
        "thread_id": 101,
        "turns": [{"role": "customer", "text": "Where is my package?"}, {"role": "agent", "text": "Checking"}]
    }) + "\n" + json.dumps({
        "thread_id": 102,
        "turns": [{"role": "customer", "text": "Package is late"}, {"role": "customer", "text": "Please help"}]
    }) + "\n")

    import_file.write_text(json.dumps({"id": "faq-order-status-late-delivery"}) + "\n")
    themes_file.write_text(json.dumps([{
        "intent_family": "order_status",
        "theme_id": "late_delivery",
        "thread_ids": ["101", "102"]
    }]))
    gold_map_file.write_text(json.dumps({"faq-order-status-late-delivery": "faq-order-status-v2"}))

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "dummy-tok\n")
    monkeypatch.setattr("retrieval_eval.search", lambda *a, **kw: ["faq-order-status-v2", "faq-other"])

    # 1. Run without --gold-map
    monkeypatch.setattr("sys.argv", [
        "retrieval_eval.py",
        "--themes", str(themes_file),
        "--threads", str(threads_file),
        "--import", str(import_file),
        "--out", str(out_file1),
        "--n", "1",
    ])
    main()
    res1 = json.load(open(out_file1))
    assert res1["report"]["n"] == 1
    # Without gold-map, expected doc was faq-order-status-late-delivery, but search returned faq-order-status-v2
    assert res1["rows"][0]["rank"] is None

    # 2. Run with --gold-map
    monkeypatch.setattr("sys.argv", [
        "retrieval_eval.py",
        "--themes", str(themes_file),
        "--threads", str(threads_file),
        "--import", str(import_file),
        "--out", str(out_file2),
        "--gold-map", str(gold_map_file),
        "--n", "1",
    ])
    main()
    res2 = json.load(open(out_file2))
    assert res2["report"]["n"] == 1
    # With gold-map, faq-order-status-late-delivery mapped to faq-order-status-v2, which was rank 1
    assert res2["rows"][0]["rank"] == 1


def test_retrieval_eval_location_default_global(tmp_path, monkeypatch):
    threads_file = tmp_path / "threads.jsonl"
    themes_file = tmp_path / "themes.json"
    import_file = tmp_path / "faq_articles_import.jsonl"
    out_file = tmp_path / "out.json"

    threads_file.write_text(json.dumps({
        "thread_id": 101,
        "turns": [{"role": "customer", "text": "Where is my package?"}, {"role": "agent", "text": "Checking"}]
    }) + "\n" + json.dumps({
        "thread_id": 102,
        "turns": [{"role": "customer", "text": "Package is late"}, {"role": "customer", "text": "Please help"}]
    }) + "\n")
    import_file.write_text(json.dumps({"id": "faq-order-status-late-delivery"}) + "\n")
    themes_file.write_text(json.dumps([{
        "intent_family": "order_status",
        "theme_id": "late_delivery",
        "thread_ids": ["101", "102"]
    }]))

    captured = {}

    def mock_search(q, url, tok, project, ds, **kwargs):
        captured["url"] = url
        captured["ds"] = ds
        return ["faq-order-status-late-delivery"]

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "dummy-tok\n")
    monkeypatch.setattr("retrieval_eval.search", mock_search)
    monkeypatch.setattr("sys.argv", [
        "retrieval_eval.py",
        "--themes", str(themes_file),
        "--threads", str(threads_file),
        "--import", str(import_file),
        "--out", str(out_file),
        "--n", "1",
    ])

    main()

    expected_url = (
        "https://discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/global/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    expected_ds = (
        "projects/susmith-fde-ticket-router/locations/global/collections/default_collection/"
        "dataStores/faq-articles"
    )
    assert captured["url"] == expected_url
    assert captured["ds"] == expected_ds


def test_retrieval_eval_location_us(tmp_path, monkeypatch):
    threads_file = tmp_path / "threads.jsonl"
    themes_file = tmp_path / "themes.json"
    import_file = tmp_path / "faq_articles_import.jsonl"
    out_file = tmp_path / "out.json"

    threads_file.write_text(json.dumps({
        "thread_id": 101,
        "turns": [{"role": "customer", "text": "Where is my package?"}, {"role": "agent", "text": "Checking"}]
    }) + "\n" + json.dumps({
        "thread_id": 102,
        "turns": [{"role": "customer", "text": "Package is late"}, {"role": "customer", "text": "Please help"}]
    }) + "\n")
    import_file.write_text(json.dumps({"id": "faq-order-status-late-delivery"}) + "\n")
    themes_file.write_text(json.dumps([{
        "intent_family": "order_status",
        "theme_id": "late_delivery",
        "thread_ids": ["101", "102"]
    }]))

    captured = {}

    def mock_search(q, url, tok, project, ds, **kwargs):
        captured["url"] = url
        captured["ds"] = ds
        return ["faq-order-status-late-delivery"]

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "dummy-tok\n")
    monkeypatch.setattr("retrieval_eval.search", mock_search)
    monkeypatch.setattr("sys.argv", [
        "retrieval_eval.py",
        "--themes", str(themes_file),
        "--threads", str(threads_file),
        "--import", str(import_file),
        "--out", str(out_file),
        "--location", "us",
        "--n", "1",
    ])

    main()

    expected_url = (
        "https://us-discoveryengine.googleapis.com/v1/projects/susmith-fde-ticket-router/"
        "locations/us/collections/default_collection/engines/support-kb-search/"
        "servingConfigs/default_search:search"
    )
    expected_ds = (
        "projects/susmith-fde-ticket-router/locations/us/collections/default_collection/"
        "dataStores/faq-articles"
    )
    assert captured["url"] == expected_url
    assert captured["ds"] == expected_ds
