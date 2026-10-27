"""Tests for centralized model configuration and guard against hard-coded model IDs.

# REQ-BILL-MODELS-001: Centralized Model Configuration Registry
# REQ-BILL-MODELS-002: Automated Hard-Coded Model Guard
# REQ-BILL-MODELS-003: Model Change & Upgrade Runbook Traceability
"""

import os
import pathlib
import re
import subprocess
import sys
from typing import List, Tuple

import pytest

from ticket_router.config.models import _parse_yaml_fallback, get, list_roles, model_id

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directories permitted to contain model ID literals (e.g. golden manifests, test fixtures, config)
ALLOWED_DIR_PARTS = {
    "config",
    "tests",
    "docs",
    ".venv",
    ".git",
    "__pycache__",
    "results",
    "labels",
    "corpus_v2",
}

# Known model IDs that must never be hard-coded into executable code outside config
MODEL_ID_PATTERNS = [
    r"gemini-3\.8-flash(?:-high)?",
    r"gemini-3\.1-pro(?:-preview)?",
    r"gemini-3\.1-flash-lite",
    r"gemini-2\.5-flash",
    r"claude-opus-5-5",
    r"claude-sonnet-5-5",
    r"openai/gpt-oss-120b-maas",
    r"semantic-ranker-512@latest",
    r"cross-encoder/ms-marco-MiniLM-L-6-v2",
]
MODEL_REGEX = re.compile("|".join(f"(?:{p})" for p in MODEL_ID_PATTERNS))


# ---------------------------------------------------------------------------
# REQ-BILL-MODELS-002: Hard-coded model ID guard scanner
# ---------------------------------------------------------------------------


def scan_repo_for_hardcoded_models() -> List[Tuple[str, int, str]]:
    """Scan all *.py, *.sh, *.yaml files outside allowed directories for model literals."""
    violations: List[Tuple[str, int, str]] = []
    target_extensions = {".py", ".sh", ".yaml", ".yml"}

    for root, dirs, files in os.walk(REPO_ROOT):
        # Prune disallowed directories from recursion
        rel_root = pathlib.Path(root).relative_to(REPO_ROOT)
        if any(part in ALLOWED_DIR_PARTS for part in rel_root.parts):
            dirs[:] = []
            continue

        for f in files:
            p = pathlib.Path(root) / f
            if p.suffix not in target_extensions:
                continue

            rel_file = p.relative_to(REPO_ROOT)
            # Extra safety check against allowed directory paths
            if any(part in ALLOWED_DIR_PARTS for part in rel_file.parts):
                continue

            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue

            for idx, line in enumerate(lines, start=1):
                match = MODEL_REGEX.search(line)
                if match:
                    violations.append((str(rel_file), idx, line.strip()))

    return violations


def test_no_hardcoded_model_literals_in_repo():
    """REQ-BILL-MODELS-002: Ensure no hardcoded model literals exist in code or scripts."""
    violations = scan_repo_for_hardcoded_models()
    if violations:
        msg_lines = [
            f"Found {len(violations)} hardcoded model literal(s) outside allowed directories:"
        ]
        for path, line_no, line in violations:
            msg_lines.append(f"  {path}:{line_no}: {line}")
        msg_lines.append(
            "\nFix: Centralize model IDs in config/models.yaml and retrieve via config.models.model_id(role)."
        )
        pytest.fail("\n".join(msg_lines))


# ---------------------------------------------------------------------------
# REQ-BILL-MODELS-001: Loader precedence, env overrides, and error handling
# ---------------------------------------------------------------------------


def test_model_id_resolves_all_standard_roles():
    """REQ-BILL-MODELS-001: Standard roles resolve to valid model ID strings."""
    expected_roles = [
        ("answer", "gemini-3.8-flash"),
        ("judge", "claude-opus-5-5"),
        ("question_gen", "claude-sonnet-5-5"),
        ("query_rewrite", "gemini-3.8-flash"),
        ("router_tier2", "gemini-3.8-flash"),
        ("tier3_multihop", "gemini-3.1-pro"),
        ("emergency", "gemini-3.1-flash-lite"),
        ("evalsvc_autorater", "gemini-3.8-flash"),
        ("evalsvc_autorater_fallback", "gemini-2.5-flash"),
        ("corpus_author", "gemini-3.1-pro-preview"),
        ("agent_authorship_tag", "gemini-3.8-flash-high"),
        ("teacher", "gemini-3.1-pro-preview"),
        ("probe_model", "gemini-3.1-pro"),
        ("cross_family_claude", "claude-opus-5-5"),
        ("cross_family_gpt_oss", "openai/gpt-oss-120b-maas"),
        ("ranking_api_model", "semantic-ranker-512@latest"),
        ("ce_reranker_base", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        ("laya_router", "router-s1-v2"),
        ("laya_reranker", "rerank-v2"),
    ]
    for role, expected_id in expected_roles:
        assert model_id(role) == expected_id, f"Role '{role}' did not match expected ID"


def test_get_role_contains_metadata():
    """REQ-BILL-MODELS-001: get(role) returns dictionary with expected metadata keys."""
    ans = get("answer")
    assert ans["id"] == "gemini-3.8-flash"
    assert ans.get("location") == "global"
    assert "notes" in ans

    teacher = get("teacher")
    assert teacher["id"] == "gemini-3.1-pro-preview"
    assert teacher.get("location") == "global"
    assert teacher.get("thinking") == "LOW"


def test_unknown_role_raises_key_error():
    """REQ-BILL-MODELS-001: Unknown model role raises KeyError with available roles listed."""
    with pytest.raises(KeyError, match="Unknown model role: 'nonexistent_role'"):
        get("nonexistent_role")

    with pytest.raises(KeyError):
        model_id("unknown_xyz")


def test_env_override_block(tmp_path, monkeypatch):
    """REQ-BILL-MODELS-001: envs[ENV] overrides base model when ENV is set."""
    custom_yaml = tmp_path / "models_override.yaml"
    custom_yaml.write_text(
        """
models:
  answer:
    id: "gemini-3.8-flash"
    location: "us-central1"
envs:
  dev:
    answer:
      id: "gemini-dev-flash"
  prod:
    answer:
      id: "gemini-prod-flash"
""",
        encoding="utf-8",
    )
    # 1. Base resolution without ENV
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert model_id("answer", config_path=custom_yaml) == "gemini-3.8-flash"

    # 2. DEV override
    monkeypatch.setenv("ENV", "dev")
    assert model_id("answer", config_path=custom_yaml) == "gemini-dev-flash"

    # 3. PROD override
    monkeypatch.setenv("ENV", "prod")
    assert model_id("answer", config_path=custom_yaml) == "gemini-prod-flash"


def test_env_var_precedence_overrides_everything(tmp_path, monkeypatch):
    """REQ-BILL-MODELS-001: MODEL_<ROLE> environment variable has highest precedence."""
    custom_yaml = tmp_path / "models_prec.yaml"
    custom_yaml.write_text(
        """
models:
  answer:
    id: "gemini-base"
envs:
  prod:
    answer:
      id: "gemini-prod"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("MODEL_ANSWER", "gemini-envvar-override")

    # Env var overrides both base and envs[prod]
    res = model_id("answer", config_path=custom_yaml)
    assert res == "gemini-envvar-override"


def test_list_roles_returns_full_map():
    """REQ-BILL-MODELS-001: list_roles() returns all configured roles and resolved IDs."""
    roles = list_roles()
    assert "answer" in roles
    assert "judge" in roles
    assert "teacher" in roles
    assert roles["answer"] == "gemini-3.8-flash"


# ---------------------------------------------------------------------------
# REQ-BILL-MODELS-003: Zero-dependency fallback parser test
# ---------------------------------------------------------------------------


def test_fallback_yaml_parser_accurately_parses_models_yaml():
    """REQ-BILL-MODELS-003: _parse_yaml_fallback parses models.yaml cleanly without PyYAML."""
    config_file = REPO_ROOT / "src" / "ticket_router" / "config" / "models.yaml"
    text = config_file.read_text(encoding="utf-8")
    parsed = _parse_yaml_fallback(text)

    assert "models" in parsed
    assert "envs" in parsed
    assert parsed["models"]["answer"]["id"] == "gemini-3.8-flash"
    assert parsed["models"]["judge"]["id"] == "claude-opus-5-5"
    assert parsed["models"]["ranking_api_model"]["id"] == "semantic-ranker-512@latest"
    assert parsed["models"]["teacher"]["thinking"] == "LOW"
    assert isinstance(parsed["envs"], dict)


def test_cli_invocation_returns_expected_id():
    """REQ-BILL-MODELS-001: python -m ticket_router.config.models <role> executes as CLI."""
    cmd = [sys.executable, "-m", "ticket_router.config.models", "answer"]
    res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "gemini-3.8-flash"
