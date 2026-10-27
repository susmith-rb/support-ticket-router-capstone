"""tests/test_platform.py — Architecture, packaging, and auth invariants.

Validates compliance with specs/platform.md:
- REQ-PLAT-001: Standard package layout and zero sys.path mutations.
- REQ-PLAT-002: Runtime authentication via Google Auth ADC with token caching.
- REQ-PLAT-003: Strict separation between runtime (src/ticket_router) and offline packages.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent


# REQ-PLAT-001
def test_zero_sys_path_mutations():
    """Verify zero sys.path.insert or sys.path.append across src, evals, corpus, experiments, and tests."""
    target_dirs = ["src", "evals", "corpus", "experiments", "tests"]
    violations = []

    for dir_name in target_dirs:
        dir_path = REPO_ROOT / dir_name
        if not dir_path.is_dir():
            continue
        for py_path in dir_path.rglob("*.py"):
            if py_path.resolve() == Path(__file__).resolve():
                continue
            text = py_path.read_text(encoding="utf-8")
            if "sys.path.insert" in text or "sys.path.append" in text:
                violations.append(str(py_path.relative_to(REPO_ROOT)))

    assert not violations, f"Found forbidden sys.path mutations in: {violations}"


# REQ-PLAT-002
def test_runtime_auth_adc_token_caching(monkeypatch):
    """Verify runtime auth uses google.auth ADC with in-memory token caching and zero subprocess calls."""
    import ticket_router.common.auth as auth_mod

    # Reset cache to clean slate
    auth_mod.reset_auth_cache()

    # Track subprocess invocations to ensure zero gcloud subprocess calls
    mock_subproc = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_subproc)

    # Mock google.auth.default and Request
    mock_creds = MagicMock()
    mock_creds.token = "mock-token-abc"
    mock_creds.valid = False

    def fake_refresh(request):
        mock_creds.token = "mock-token-refreshed"
        mock_creds.valid = True

    mock_creds.refresh = MagicMock(side_effect=fake_refresh)

    monkeypatch.setattr(
        auth_mod.google.auth,
        "default",
        MagicMock(return_value=(mock_creds, "mock-project")),
    )

    # First call: refreshes token
    token1 = auth_mod.get_access_token()
    assert token1 == "mock-token-refreshed"
    assert mock_creds.refresh.call_count == 1
    assert mock_subproc.call_count == 0

    # Second call: uses cached valid token without calling refresh
    token2 = auth_mod.get_access_token()
    assert token2 == "mock-token-refreshed"
    assert mock_creds.refresh.call_count == 1
    assert mock_subproc.call_count == 0

    # Reset cache
    auth_mod.reset_auth_cache()
    mock_creds.valid = False
    token3 = auth_mod.get_access_token()
    assert token3 == "mock-token-refreshed"
    assert mock_creds.refresh.call_count == 2
    assert mock_subproc.call_count == 0


# REQ-PLAT-002
def test_zero_print_access_token_shelling():
    """Verify zero print-access-token subprocess shelling across src, evals, corpus, experiments."""
    target_dirs = ["src", "evals", "corpus", "experiments"]
    violations = []

    for dir_name in target_dirs:
        dir_path = REPO_ROOT / dir_name
        if not dir_path.is_dir():
            continue
        for py_path in dir_path.rglob("*.py"):
            text = py_path.read_text(encoding="utf-8")
            if "print-access-token" in text:
                violations.append(str(py_path.relative_to(REPO_ROOT)))

    assert not violations, f"Found forbidden 'print-access-token' in: {violations}"


# REQ-PLAT-003
def test_runtime_offline_isolation():
    """Verify src/ticket_router code never imports from evals, corpus, experiments, or training."""
    src_dir = REPO_ROOT / "src" / "ticket_router"
    forbidden_roots = {"evals", "corpus", "experiments", "training"}
    violations = []

    for py_file in src_dir.rglob("*.py"):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg in forbidden_roots:
                        violations.append((rel, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_pkg = node.module.split(".")[0]
                if root_pkg in forbidden_roots:
                    violations.append((rel, node.lineno, node.module))

    assert not violations, f"Runtime code illegally imports offline modules: {violations}"
