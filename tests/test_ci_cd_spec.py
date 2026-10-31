"""Specification tests for CI/CD pipelines and coverage enforcement.

# REQ-CICD-001: Declarative CI/CD Pipeline Specifications
# REQ-CICD-002: Automated Pull-Request and Push CI Gate
# REQ-CICD-003: Corpus Reindexing and Retrieval Regression Gate
# REQ-CICD-004: Nightly Retrieval Health and Drift Evaluation
# REQ-CICD-005: Dedicated Least-Privilege Build and Scheduler Service Accounts
# REQ-CICD-006: Cloud Build v2 GitHub Connection and Repository Resources
# REQ-CICD-007: Cloud Scheduler Nightly Evaluation Target Configuration
# REQ-CICD-008: Static Security and Secret Leakage Prevention
# REQ-CICD-009: Change-Aware Golden Retrieval Evaluation Gate Execution
# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "specs" / "ci-cd.md"
CICD_DIR = REPO_ROOT / "cicd"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

EXPECTED_CICD_REQS = [f"REQ-CICD-{i:03d}" for i in range(1, 11)]


def _read_spec() -> str:
    assert SPEC_PATH.exists(), f"Spec file not found at {SPEC_PATH}"
    return SPEC_PATH.read_text(encoding="utf-8")


def test_ci_cd_spec_file_and_requirements():
    """Verify specs/ci-cd.md exists and defines REQ-CICD-001 through REQ-CICD-010."""
    text = _read_spec()
    for req in EXPECTED_CICD_REQS:
        assert f"### {req}" in text or f"| `{req}` |" in text, (
            f"Missing {req} definition in specs/ci-cd.md"
        )


def test_ci_cd_coverage_enforcement_contract():
    """Verify REQ-CICD-010: line and branch test coverage enforcement >= 80%."""
    cb_path = CICD_DIR / "cloudbuild.yaml"
    assert cb_path.is_file(), "Missing cicd/cloudbuild.yaml"
    cb_content = cb_path.read_text(encoding="utf-8")

    assert PYPROJECT_PATH.is_file(), "Missing pyproject.toml"
    pyproject_content = PYPROJECT_PATH.read_text(encoding="utf-8")

    # CloudBuild pipeline asserts
    assert "--cov=src/ticket_router" in cb_content or "--cov" in cb_content
    assert "--cov-branch" in cb_content
    assert "--cov-fail-under=80" in cb_content or "fail_under = 80" in pyproject_content

    # pyproject.toml configuration asserts
    assert "[tool.coverage.run]" in pyproject_content
    assert "branch = true" in pyproject_content
    assert "[tool.coverage.report]" in pyproject_content
    assert "fail_under = 80" in pyproject_content


def test_ci_cd_pipeline_declarations():
    """Verify REQ-CICD-001: all declarative Cloud Build pipelines exist."""
    assert (CICD_DIR / "cloudbuild.yaml").is_file()
    assert (CICD_DIR / "cloudbuild-reindex.yaml").is_file()
    assert (CICD_DIR / "cloudbuild-nightly.yaml").is_file()


def test_ci_cd_change_aware_gate_spec():
    """Verify REQ-CICD-009: change-aware script and state recording contract."""
    cb_path = CICD_DIR / "cloudbuild.yaml"
    cb_content = cb_path.read_text(encoding="utf-8")

    assert "detect-changes" in cb_content
    assert "_RUN_EVAL: 'auto'" in cb_content
    assert "record-ci-state" in cb_content
