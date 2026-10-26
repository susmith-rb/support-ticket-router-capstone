#!/usr/bin/env python3
"""ci_changes.py — Change-aware path detection for CI/CD pipelines.

Inspects git diffs against previous commit or PR target branch to determine
which pipeline subsystems require execution:
- Tests: ALWAYS run (invariant)
- Golden Eval Gate (RETRIEVAL_CHANGED): Run only if retrieval-relevant files changed
  (rag/corpus_v2/**, top-level rag/*.py, deploy/terraform/.../agent-search/**; docs/results excluded)
- Terraform checks/plan: Run only if deploy/terraform/** changed
- FAQ corpus reindex: Run only if rag/corpus_v2/** changed

If git diff is unavailable or fails (shallow clone, detached HEAD, unknown base),
gracefully falls back to running all stages (conservative safe posture).
"""

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Dict, List, Optional, Sequence


def is_retrieval_file(path: str) -> bool:
    """Return True if path affects retrieval or evaluation."""
    norm = path.strip().replace("\\", "/")
    if norm.endswith(".md"):
        return False
    if norm.startswith("rag/results/") or norm == "rag/results":
        return False
    if norm.startswith("rag/labels/") or norm == "rag/labels":
        return False
    if norm.startswith("rag/corpus_v2/") or norm == "rag/corpus_v2":
        return True
    if (
        norm.startswith("deploy/terraform/single-project/modules/agent-search/")
        or norm == "deploy/terraform/single-project/modules/agent-search"
    ):
        return True
    parts = norm.split("/")
    if len(parts) == 2 and parts[0] == "rag" and parts[1].endswith(".py"):
        return True
    return False


def detect_changes(changed_files: Sequence[str]) -> Dict[str, bool]:
    """Pure function mapping a sequence of changed paths to subsystem run flags."""
    normalized = [f.strip().replace("\\", "/") for f in changed_files if f.strip()]

    terraform_changed = any(
        f.startswith("deploy/terraform/") or f == "deploy/terraform" for f in normalized
    )
    corpus_changed = any(
        f.startswith("rag/corpus_v2/") or f == "rag/corpus_v2" for f in normalized
    )
    router_s1_changed = any(
        f.startswith("training/router_s1/") or f == "training/router_s1" for f in normalized
    )
    retrieval_changed = any(is_retrieval_file(f) for f in normalized)

    return {
        "TESTS_ALWAYS_RUN": True,
        "RETRIEVAL_CHANGED": retrieval_changed,
        "TERRAFORM_CHANGED": terraform_changed,
        "CORPUS_CHANGED": corpus_changed,
        "ROUTER_S1_CHANGED": router_s1_changed,
    }


def get_changed_files_from_git(base_ref: Optional[str] = None) -> Optional[List[str]]:
    """Retrieve changed file paths via git diff, returning None on any git failure."""
    candidates = []
    if base_ref:
        candidates.append([base_ref, "HEAD"])
    candidates.extend([
        ["HEAD~1", "HEAD"],
        ["origin/main...HEAD"],
        ["main...HEAD"],
    ])

    for args in candidates:
        cmd = ["git", "diff", "--name-only"] + args
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            return files
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return None


def resolve_flags(changed_files: Optional[Sequence[str]]) -> Dict[str, bool]:
    """Resolve subsystem execution flags, falling back to 'run all' if files are None."""
    if changed_files is None:
        # Conservative fallback: run all subsystems if changes could not be determined
        return {
            "TESTS_ALWAYS_RUN": True,
            "RETRIEVAL_CHANGED": True,
            "TERRAFORM_CHANGED": True,
            "CORPUS_CHANGED": True,
            "ROUTER_S1_CHANGED": True,
        }
    return detect_changes(changed_files)


def write_env_file(flags: Dict[str, bool], env_path: Path) -> None:
    """Write environment variable assignments to a shell-sourceable env file."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"export {key}={'true' if val else 'false'}"
        for key, val in sorted(flags.items())
    ]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Detect changed paths for CI/CD pipeline")
    parser.add_argument("--base-ref", default=None, help="Base commit/branch to diff against")
    parser.add_argument("--files", nargs="*", default=None, help="Explicit list of files (for testing/mocking)")
    parser.add_argument("--env-file", default="/workspace/changes.env", help="Path to write changes.env")
    parser.add_argument("--json", action="store_true", help="Print JSON output to stdout")
    args = parser.parse_args(argv)

    if args.files is not None:
        changed_files = args.files
    else:
        changed_files = get_changed_files_from_git(args.base_ref)

    flags = resolve_flags(changed_files)

    print("=== CHANGE-AWARE SUBSYSTEM FLAGS ===")
    for k, v in sorted(flags.items()):
        print(f"  {k} = {v}")

    env_path = Path(args.env_file)
    write_env_file(flags, env_path)
    print(f"Subsystem flags written to: {env_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
