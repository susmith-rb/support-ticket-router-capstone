import json
import pathlib
from corpus.augment_paraphrases import (
    apply_disambiguation_to_body,
    verify_no_leakage,
)
from corpus.publish_faq import lint


def test_apply_disambiguation_to_body_idempotent():
    initial_body = """# Shipping Delays

Also asked as:
- why is my package delayed?
- what is standard shipping speed?

Shipping takes 3-5 business days.
"""
    scope_line = "Scope: This article covers standard shipping speeds. For late packages, see \"Where is my order?\"."
    new_questions = [
        "why does shipping take extra days before dispatching?",
        "does fast shipping include weekend delivery?",
    ]
    removed_questions = [
        "why is my package delayed?"
    ]

    # First application
    out1 = apply_disambiguation_to_body(
        initial_body,
        scope_line=scope_line,
        new_questions=new_questions,
        removed_questions=removed_questions,
    )
    assert scope_line in out1
    assert "- why does shipping take extra days before dispatching?" in out1
    assert "- what is standard shipping speed?" in out1
    assert "- why is my package delayed?" not in out1
    assert "Shipping takes 3-5 business days." in out1

    # Second application (idempotence)
    out2 = apply_disambiguation_to_body(
        out1,
        scope_line=scope_line,
        new_questions=new_questions,
        removed_questions=removed_questions,
    )
    assert out2 == out1


def test_disambiguation_v1_leakage_and_lint():
    # REQ-RAG-CORPUS-018: Index-Side Disambiguation Questions, Scope Statements, and Leakage Guards
    """Verify REQ-RAG-CORPUS-018:
    1. disambiguation_v1.json exists and has valid structure.
    2. Zero leakage into dev.jsonl or test.jsonl.
    3. faq_v2d articles have scope lines and pass policy lint.
    """
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    scratch = pathlib.Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag")
    disam_path = repo / "rag/corpus_v2/agent_decisions/disambiguation_v1.json"
    dev_path = scratch / "eval/dev.jsonl"
    test_path = scratch / "eval/test.jsonl"
    v2d_docs = scratch / "faq_v2d/docs"

    assert disam_path.exists(), f"Missing {disam_path}"
    with open(disam_path, "r", encoding="utf-8") as f:
        disam_data = json.load(f)

    assert len(disam_data) >= 8, f"Expected at least 8 target articles, got {len(disam_data)}"

    # Leakage verification
    verify_no_leakage(disam_data, dev_path, test_path)

    # Check that augmented docs in faq_v2d have scope lines and pass lint
    if v2d_docs.exists():
        docs = {f.stem: f.read_text(encoding="utf-8") for f in v2d_docs.glob("*.txt")}
        assert len(docs) == 50, f"Expected 50 documents, got {len(docs)}"

        # Every target doc must have its scope line
        for doc_id, data in disam_data.items():
            assert doc_id in docs, f"Doc {doc_id} not in v2d docs"
            body = docs[doc_id]
            scope = data.get("scope_line")
            if scope:
                assert scope in body, f"Scope line missing from {doc_id}"

        # Policy lint must pass cleanly
        bad = lint(docs)
        assert not bad, f"Policy lint violations in faq_v2d: {bad}"
