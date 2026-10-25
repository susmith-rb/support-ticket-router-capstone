"""Unit tests for change-aware CI path detection (scripts/ci_changes.py).

Validates that:
- detect_changes correctly identifies modified subsystems.
- Tests and Golden Eval Gate are marked as always running.
- resolve_flags falls back to running all subsystems if changed files are None.
- write_env_file generates valid shell environment exports.
"""

from scripts.ci_changes import detect_changes, resolve_flags, write_env_file


# REQ-CICD-002
def test_detect_changes_only_python_tests():
    """Modifying only unit tests should not trigger terraform or corpus reindexing."""
    files = ["tests/test_spec_traceability.py", "rag/search_client.py"]
    flags = detect_changes(files)

    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["EVAL_GATE_ALWAYS_RUN"] is True
    assert flags["TERRAFORM_CHANGED"] is False
    assert flags["CORPUS_CHANGED"] is False
    assert flags["ROUTER_S1_CHANGED"] is False


# REQ-CICD-002
# REQ-CICD-003
def test_detect_changes_terraform_and_corpus():
    """Modifying terraform and corpus files must trigger respective flags."""
    files = [
        "deploy/terraform/single-project/main.tf",
        "rag/corpus_v2/manifest.jsonl",
        "README.md",
    ]
    flags = detect_changes(files)

    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["EVAL_GATE_ALWAYS_RUN"] is True
    assert flags["TERRAFORM_CHANGED"] is True
    assert flags["CORPUS_CHANGED"] is True
    assert flags["ROUTER_S1_CHANGED"] is False


# REQ-CICD-002
def test_detect_changes_router_s1():
    """Modifying training pipeline files must set ROUTER_S1_CHANGED."""
    files = ["training/router_s1/pipeline/evaluate.py"]
    flags = detect_changes(files)

    assert flags["ROUTER_S1_CHANGED"] is True
    assert flags["TERRAFORM_CHANGED"] is False
    assert flags["CORPUS_CHANGED"] is False


# REQ-CICD-002
def test_resolve_flags_fallback():
    """If changed files cannot be determined (None), all subsystems must run."""
    flags = resolve_flags(None)
    assert flags["TESTS_ALWAYS_RUN"] is True
    assert flags["EVAL_GATE_ALWAYS_RUN"] is True
    assert flags["TERRAFORM_CHANGED"] is True
    assert flags["CORPUS_CHANGED"] is True
    assert flags["ROUTER_S1_CHANGED"] is True


# REQ-CICD-002
def test_write_env_file(tmp_path):
    """write_env_file must output export KEY=val lines."""
    flags = {
        "TESTS_ALWAYS_RUN": True,
        "TERRAFORM_CHANGED": False,
        "CORPUS_CHANGED": True,
    }
    env_file = tmp_path / "changes.env"
    write_env_file(flags, env_file)

    assert env_file.is_file()
    content = env_file.read_text(encoding="utf-8")
    assert "export TESTS_ALWAYS_RUN=true\n" in content
    assert "export TERRAFORM_CHANGED=false\n" in content
    assert "export CORPUS_CHANGED=true\n" in content
