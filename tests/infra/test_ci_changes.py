"""Unit tests for change-aware CI path detection (scripts/ci_changes.py).

Validates that:
- detect_changes correctly identifies modified subsystems for pure file lists.
- compute_fingerprints generates deterministic SHA-256 fingerprints per subsystem.
- Subsystem isolation: changes to one subsystem do not trigger unrelated subsystems.
- Docs, results, and label changes do not flip retrieval fingerprints.
- CLI --state-in with missing/empty baseline flags all subsystems as changed.
- CLI --keep-prev preserves specified subsystem fingerprints across state updates.
- write_env_file generates valid shell environment exports.
"""

from scripts.ci_changes import (
    compute_fingerprints,
    detect_changes,
    load_state,
    main,
    resolve_flags,
    write_env_file,
    write_state,
)


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


# REQ-CICD-009
def test_compute_fingerprints_deterministic(tmp_path):
    """compute_fingerprints must be stable across multiple runs on identical files."""
    tf_dir = tmp_path / "deploy" / "terraform" / "single-project"
    tf_dir.mkdir(parents=True)
    (tf_dir / "main.tf").write_text("resource null_resource x {}", encoding="utf-8")

    corpus_dir = tmp_path / "rag" / "corpus_v2"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "faq.txt").write_text("Sample FAQ content", encoding="utf-8")

    fp1 = compute_fingerprints(tmp_path)
    fp2 = compute_fingerprints(tmp_path)

    assert fp1 == fp2
    assert set(fp1.keys()) == {"retrieval", "corpus", "terraform"}
    for k, v in fp1.items():
        assert len(v) == 64  # SHA-256 hex string


# REQ-CICD-009
def test_fingerprints_subsystem_isolation_and_doc_exclusions(tmp_path):
    """Changes to one subsystem only affect its fingerprint; doc edits don't flip retrieval."""
    tf_dir = tmp_path / "deploy" / "terraform" / "single-project"
    tf_dir.mkdir(parents=True)
    tf_file = tf_dir / "main.tf"
    tf_file.write_text("resource null_resource x {}", encoding="utf-8")

    corpus_dir = tmp_path / "rag" / "corpus_v2"
    corpus_dir.mkdir(parents=True)
    corpus_file = corpus_dir / "faq.txt"
    corpus_file.write_text("FAQ 1", encoding="utf-8")

    rag_dir = tmp_path / "rag"
    gate_file = rag_dir / "ci_eval_gate.py"
    gate_file.write_text("print('gate')", encoding="utf-8")

    doc_file = rag_dir / "README.md"
    doc_file.write_text("# Documentation", encoding="utf-8")

    base_fps = compute_fingerprints(tmp_path)

    # 1. Edit docs: none of retrieval, corpus, or terraform should change
    doc_file.write_text("# Updated Documentation", encoding="utf-8")
    doc_fps = compute_fingerprints(tmp_path)
    assert doc_fps == base_fps

    # 2. Edit terraform: only terraform changes
    tf_file.write_text("resource null_resource y {}", encoding="utf-8")
    tf_fps = compute_fingerprints(tmp_path)
    assert tf_fps["terraform"] != base_fps["terraform"]
    assert tf_fps["corpus"] == base_fps["corpus"]
    assert tf_fps["retrieval"] == base_fps["retrieval"]

    # 3. Edit standalone rag script: only retrieval changes
    gate_file.write_text("print('updated gate')", encoding="utf-8")
    rag_fps = compute_fingerprints(tmp_path)
    assert rag_fps["retrieval"] != tf_fps["retrieval"]
    assert rag_fps["corpus"] == tf_fps["corpus"]
    assert rag_fps["terraform"] == tf_fps["terraform"]

    # 4. Edit corpus: both corpus and retrieval change
    corpus_file.write_text("FAQ 2", encoding="utf-8")
    corpus_fps = compute_fingerprints(tmp_path)
    assert corpus_fps["corpus"] != rag_fps["corpus"]
    assert corpus_fps["retrieval"] != rag_fps["retrieval"]
    assert corpus_fps["terraform"] == rag_fps["terraform"]


# REQ-CICD-009
def test_cli_state_in_missing_triggers_all(tmp_path):
    """Missing or empty --state-in marks all subsystems as changed and writes state-out."""
    corpus_dir = tmp_path / "rag" / "corpus_v2"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "faq.txt").write_text("FAQ", encoding="utf-8")

    env_file = tmp_path / "changes.env"
    state_out = tmp_path / "state_new.json"
    missing_state = tmp_path / "nonexistent.json"

    ret = main([
        "--root", str(tmp_path),
        "--state-in", str(missing_state),
        "--state-out", str(state_out),
        "--env-file", str(env_file),
    ])
    assert ret == 0

    assert state_out.is_file()
    saved_state = load_state(state_out)
    assert "retrieval" in saved_state
    assert "corpus" in saved_state
    assert "terraform" in saved_state

    env_content = env_file.read_text(encoding="utf-8")
    assert "export RETRIEVAL_CHANGED=true\n" in env_content
    assert "export CORPUS_CHANGED=true\n" in env_content
    assert "export TERRAFORM_CHANGED=true\n" in env_content


# REQ-CICD-009
def test_cli_state_in_matching_skips_all(tmp_path):
    """When current fingerprints match --state-in, all subsystem flags are false."""
    corpus_dir = tmp_path / "rag" / "corpus_v2"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "faq.txt").write_text("FAQ", encoding="utf-8")

    current_fps = compute_fingerprints(tmp_path)
    state_in = tmp_path / "state_prev.json"
    write_state(current_fps, state_in)

    env_file = tmp_path / "changes.env"
    state_out = tmp_path / "state_new.json"

    ret = main([
        "--root", str(tmp_path),
        "--state-in", str(state_in),
        "--state-out", str(state_out),
        "--env-file", str(env_file),
    ])
    assert ret == 0

    env_content = env_file.read_text(encoding="utf-8")
    assert "export RETRIEVAL_CHANGED=false\n" in env_content
    assert "export CORPUS_CHANGED=false\n" in env_content
    assert "export TERRAFORM_CHANGED=false\n" in env_content


# REQ-CICD-009
def test_cli_keep_prev_subsystem(tmp_path):
    """--keep-prev preserves specified subsystem fingerprint in state-out."""
    rag_dir = tmp_path / "rag"
    rag_dir.mkdir(parents=True)
    gate_file = rag_dir / "ci_eval_gate.py"
    gate_file.write_text("v1", encoding="utf-8")

    initial_fps = compute_fingerprints(tmp_path)
    state_in = tmp_path / "state_prev.json"
    write_state(initial_fps, state_in)

    # Modify retrieval file so its new fingerprint differs
    gate_file.write_text("v2", encoding="utf-8")
    state_out = tmp_path / "state_new.json"

    ret = main([
        "--root", str(tmp_path),
        "--state-in", str(state_in),
        "--state-out", str(state_out),
        "--keep-prev", "retrieval",
    ])
    assert ret == 0

    saved_state = load_state(state_out)
    # Retrieval fingerprint must be kept from state_in (initial_fps)
    assert saved_state["retrieval"] == initial_fps["retrieval"]
