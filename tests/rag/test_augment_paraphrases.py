import json
import pathlib
import pytest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "rag"))

from augment_paraphrases import (
    merge_paraphrases_into_body,
    normalize_text,
    verify_no_leakage,
)
from publish_faq import lint


def test_normalize_text():
    raw = "@AmazonHelp why is my [ORDER] not delivered? #urgent [URL]"
    norm = normalize_text(raw)
    assert "@" not in norm
    assert "[order]" not in norm
    assert "#" not in norm
    assert norm == "why is my not delivered urgent"


def test_merge_paraphrases_into_body_idempotent():
    initial_body = """# Return Policy

Also asked as:
- how do I return an item?
- what is the return window?

Items can be returned within 30 days of delivery.
"""
    new_paras = [
        "can I return an item after 30 days?",
        "how do I send back a return?",
    ]

    # First merge
    merged_1 = merge_paraphrases_into_body(initial_body, new_paras)
    assert "- how do I return an item?" in merged_1
    assert "- can I return an item after 30 days?" in merged_1
    assert "- how do I send back a return?" in merged_1
    assert "Items can be returned within 30 days of delivery." in merged_1

    # Second merge (idempotence)
    merged_2 = merge_paraphrases_into_body(merged_1, new_paras)
    assert merged_2 == merged_1


def test_merge_paraphrases_into_body_without_existing_section():
    initial_body = """# Return Policy

Items can be returned within 30 days of delivery.
"""
    new_paras = ["how do I return an item?"]
    merged = merge_paraphrases_into_body(initial_body, new_paras)
    assert "Also asked as:" in merged
    assert "- how do I return an item?" in merged
    assert "Items can be returned within 30 days of delivery." in merged


def test_verify_no_leakage_clean(tmp_path):
    dev_path = tmp_path / "dev.jsonl"
    test_path = tmp_path / "test.jsonl"

    dev_path.write_text(json.dumps({"thread_id": "100", "query": "hello world"}) + "\n")
    test_path.write_text(json.dumps({"thread_id": "200", "query": "test query"}) + "\n")

    clean_data = {
        "faq-doc-1": {
            "paraphrases": ["completely unique question"],
            "source_thread_ids": ["300", "400"],
        }
    }
    # Should not raise
    verify_no_leakage(clean_data, dev_path, test_path)


def test_verify_no_leakage_flags_thread_id(tmp_path):
    dev_path = tmp_path / "dev.jsonl"
    test_path = tmp_path / "test.jsonl"

    dev_path.write_text(json.dumps({"thread_id": "100", "query": "hello world"}) + "\n")
    test_path.write_text(json.dumps({"thread_id": "200", "query": "test query"}) + "\n")

    leaked_data = {
        "faq-doc-1": {
            "paraphrases": ["completely unique question"],
            "source_thread_ids": ["100"],  # in dev!
        }
    }
    with pytest.raises(ValueError, match="LEAKAGE VIOLATION: Source thread IDs found"):
        verify_no_leakage(leaked_data, dev_path, test_path)


def test_verify_no_leakage_flags_query_duplicate(tmp_path):
    dev_path = tmp_path / "dev.jsonl"
    test_path = tmp_path / "test.jsonl"

    dev_path.write_text(json.dumps({"thread_id": "100", "query": "Where is my late order?"}) + "\n")
    test_path.write_text(json.dumps({"thread_id": "200", "query": "test query"}) + "\n")

    leaked_data = {
        "faq-doc-1": {
            "paraphrases": ["where is my late order?"],  # match!
            "source_thread_ids": ["300"],
        }
    }
    with pytest.raises(ValueError, match="LEAKAGE VIOLATION: Paraphrase text matches query"):
        verify_no_leakage(leaked_data, dev_path, test_path)


def test_paraphrases_v1_leakage_and_lint():
    """Verify that the actual checked-in paraphrases_v1.json passes leakage check and lint against the active docs.

    # REQ-RAG-CORPUS-017
    # REQ-RAG-RETR-014
    """
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    scratch = pathlib.Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag")
    
    para_path = repo / "rag/corpus_v2/agent_decisions/paraphrases_v1.json"
    dev_path = scratch / "eval/dev.jsonl"
    test_path = scratch / "eval/test.jsonl"

    assert para_path.exists()
    paraphrases_data = json.loads(para_path.read_text(encoding="utf-8"))

    # Leakage check on live dev and test splits
    verify_no_leakage(paraphrases_data, dev_path, test_path)

    # Check that merging into live docs produces clean policy lint
    docs_dir = scratch / "faq_v2/docs"
    if docs_dir.exists():
        docs = {}
        for p in docs_dir.glob("*.txt"):
            body = p.read_text(encoding="utf-8")
            if p.stem in paraphrases_data:
                body = merge_paraphrases_into_body(body, paraphrases_data[p.stem]["paraphrases"])
            docs[p.stem] = body
        bad = lint(docs)
        assert not bad, f"Policy lint violations found: {bad}"
