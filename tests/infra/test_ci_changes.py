"""Unit tests for change-aware CI path detection (scripts/ci_changes.py).

Validates that:
- detect_changes correctly identifies modified subsystems.
- Tests are marked as always running.
- Golden Eval Gate (RETRIEVAL_CHANGED) runs on retrieval files and skips on docs/unrelated.
- resolve_flags falls back to running all subsystems if changed files are None.
- write_env_file generates valid shell environment exports.
"""

from scripts.ci_changes import detect_changes, resolve_flags, write_env_file


# REQ-CICD-002
# REQ-CICD-009
def test_detect_changes_only_python_tests():
    """Modifying only unit tests should not trigger terraform, corpus, or retrieval eval."""
    files = ["tests/test_spec_traceability.py", "scripts/ci_changes.py"]
    flags = detect_changes(files)

    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["RETRIEVAL_CHANGED"] is False
    assert flags["TERRAFORM_CHANGED"] is False
    assert flags["CORPUS_CHANGED"] is False
    assert flags["ROUTER_S1_CHANGED"] is False


# REQ-CICD-002
# REQ-CICD-003
# REQ-CICD-009
def test_detect_changes_terraform_and_corpus():
    """Modifying terraform and corpus files must trigger respective flags."""
    files = [
        "deploy/terraform/single-project/main.tf",
        "rag/corpus_v2/manifest.jsonl",
        "README.md",
    ]
    flags = detect_changes(files)

    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["RETRIEVAL_CHANGED"] is True
    assert flags["TERRAFORM_CHANGED"] is True
    assert flags["CORPUS_CHANGED"] is True
    assert flags["ROUTER_S1_CHANGED"] is False


# REQ-CICD-009
def test_detect_changes_retrieval_paths():
    """Retrieval paths (corpus_v2, top-level rag/*.py, agent-search module) set RETRIEVAL_CHANGED."""
    test_cases = [
        ["rag/ci_eval_gate.py"],
        ["rag/search_client.py"],
        ["rag/publish_faq.py"],
        ["rag/corpus_v2/faq_data.jsonl"],
        ["deploy/terraform/single-project/modules/agent-search/main.tf"],
    ]
    for paths in test_cases:
        flags = detect_changes(paths)
        assert flags["RETRIEVAL_CHANGED"] is True, f"Expected RETRIEVAL_CHANGED for {paths}"


# REQ-CICD-009
def test_detect_changes_non_retrieval_exclusions():
    """Docs, eval results, labels, and unrelated terraform must NOT set RETRIEVAL_CHANGED."""
    files = [
        "rag/README.md",
        "rag/results/eval_gate_summary.json",
        "rag/labels/test_labels.jsonl",
        "deploy/terraform/single-project/modules/cloudbuild/main.tf",
        "docs/architecture.md",
    ]
    flags = detect_changes(files)
    assert flags["RETRIEVAL_CHANGED"] is False


# REQ-CICD-002
def test_detect_changes_router_s1():
    """Modifying training pipeline files must set ROUTER_S1_CHANGED."""
    files = ["training/router_s1/pipeline/evaluate.py"]
    flags = detect_changes(files)

    assert flags["ROUTER_S1_CHANGED"] is True
    assert flags["RETRIEVAL_CHANGED"] is False
    assert flags["TERRAFORM_CHANGED"] is False
    assert flags["CORPUS_CHANGED"] is False


# REQ-CICD-002
# REQ-CICD-009
def test_resolve_flags_fallback():
    """If changed files cannot be determined (None), all subsystems must run."""
    flags = resolve_flags(None)
    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["RETRIEVAL_CHANGED"] is True
    assert flags["TERRAFORM_CHANGED"] is True
    assert flags["CORPUS_CHANGED"] is True
    assert flags["ROUTER_S1_CHANGED"] is True


# REQ-CICD-002
def test_write_env_file(tmp_path):
    """write_env_file must output export KEY=val lines."""
    flags = {
        "TESTS_ALWAYS_RUN": True,
        "RETRIEVAL_CHANGED": True,
        "TERRAFORM_CHANGED": False,
        "CORPUS_CHANGED": True,
    }
    env_file = tmp_path / "changes.env"
    write_env_file(flags, env_file)

    assert env_file.is_file()
    content = env_file.read_text(encoding="utf-8")
    assert "export TESTS_ALWAYS_RUN=true\n" in content
    assert "export RETRIEVAL_CHANGED=true\n" in content
    assert "export TERRAFORM_CHANGED=false\n" in content
    assert "export CORPUS_CHANGED=true\n" in content
