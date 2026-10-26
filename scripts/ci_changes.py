#!/usr/bin/env python3
"""ci_changes.py — Change-aware path detection for CI/CD pipelines.

Inspects content fingerprints against the last successful main build state to
determine which pipeline subsystems require execution:
- Tests: ALWAYS run (invariant)
- Golden Eval Gate (RETRIEVAL_CHANGED): Run only if retrieval-relevant files changed
  (rag/corpus_v2/**, top-level rag/*.py, deploy/terraform/.../agent-search/**; docs/results excluded)
- Terraform checks/plan (TERRAFORM_CHANGED): Run only if deploy/terraform/** changed
- FAQ corpus reindex (CORPUS_CHANGED): Run only if rag/corpus_v2/** changed

State is stored in GCS as a JSON dictionary of SHA-256 content fingerprints.
When --state-in is omitted, falls back to pure file list matching (--files)
or git diff inspection for local development.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Union


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
    if (
        norm.startswith("src/ticket_router/retrieval/")
        or norm == "src/ticket_router/retrieval"
    ):
        return True
    if norm.startswith("evals/") or norm == "evals":
        return True
    if norm.startswith("corpus/") or norm == "corpus":
        return True
    if norm.startswith("experiments/") or norm == "experiments":
        return True
    parts = norm.split("/")
    if len(parts) == 2 and parts[0] == "rag" and parts[1].endswith(".py"):
        return True
    return False


def is_terraform_file(path: str) -> bool:
    """Return True if path is a tracked Terraform file (excluding .terraform/ and state)."""
    norm = path.strip().replace("\\", "/")
    if not (norm.startswith("deploy/terraform/") or norm == "deploy/terraform"):
        return False
    parts = norm.split("/")
    if ".terraform" in parts:
        return False
    if ".tfstate" in norm:
        return False
    return True


def is_corpus_file(path: str) -> bool:
    """Return True if path is part of FAQ corpus v2."""
    norm = path.strip().replace("\\", "/")
    return norm.startswith("rag/corpus_v2/") or norm == "rag/corpus_v2"


def is_valid_candidate_file(relpath: str) -> bool:
    """Filter out VCS directories and Python bytecode."""
    parts = relpath.split("/")
    if ".git" in parts or "__pycache__" in parts:
        return False
    if relpath.endswith(".pyc"):
        return False
    return True


def get_repo_files(root: Path) -> List[str]:
    """List tracked files via git ls-files with fallback to filesystem walk."""
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        files = [
            line.strip().replace("\\", "/")
            for line in res.stdout.splitlines()
            if line.strip()
        ]
        if files:
            return files
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback to filesystem walk
    files = []
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            files.append(rel)
    return files


def compute_fingerprint_for_files(root: Path, file_list: Sequence[str]) -> str:
    """Compute deterministic SHA-256 fingerprint over sorted (relpath, sha256(bytes))."""
    items = []
    for rel in sorted(file_list):
        file_path = root / rel
        if file_path.is_file():
            content = file_path.read_bytes()
            f_hash = hashlib.sha256(content).hexdigest()
            items.append((rel, f_hash))
    hasher = hashlib.sha256()
    for rel, f_hash in items:
        hasher.update(f"{rel}:{f_hash}\n".encode("utf-8"))
    return hasher.hexdigest()


def compute_fingerprints(root: Union[str, Path] = ".") -> Dict[str, str]:
    """Compute SHA-256 content fingerprints for retrieval, corpus, and terraform subsystems."""
    root_path = Path(root).resolve()
    all_files = [f for f in get_repo_files(root_path) if is_valid_candidate_file(f)]

    retrieval_files = [f for f in all_files if is_retrieval_file(f)]
    corpus_files = [f for f in all_files if is_corpus_file(f)]
    terraform_files = [f for f in all_files if is_terraform_file(f)]

    return {
        "retrieval": compute_fingerprint_for_files(root_path, retrieval_files),
        "corpus": compute_fingerprint_for_files(root_path, corpus_files),
        "terraform": compute_fingerprint_for_files(root_path, terraform_files),
    }


def load_state(path: Optional[Union[str, Path]]) -> Dict[str, str]:
    """Load fingerprint state from JSON file; missing, empty, or invalid returns {}."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def write_state(fps: Dict[str, str], path: Union[str, Path]) -> None:
    """Write fingerprint state to JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fps, indent=2) + "\n", encoding="utf-8")


def detect_changes(changed_files: Sequence[str]) -> Dict[str, bool]:
    """Pure function mapping a sequence of changed paths to subsystem run flags."""
    normalized = [f.strip().replace("\\", "/") for f in changed_files if f.strip()]

    terraform_changed = any(is_terraform_file(f) for f in normalized)
    corpus_changed = any(is_corpus_file(f) for f in normalized)
    router_s1_changed = any(
        f.startswith("training/router_s1/") or f == "training/router_s1"
        for f in normalized
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
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--state-in", default=None, help="Path to previous ci_state.json")
    parser.add_argument("--state-out", default=None, help="Path to write new ci_state.json")
    parser.add_argument(
        "--keep-prev",
        nargs="*",
        default=[],
        help="Subsystem names whose previous fingerprint to preserve in --state-out",
    )
    parser.add_argument("--base-ref", default=None, help="Base commit/branch to diff against")
    parser.add_argument(
        "--files", nargs="*", default=None, help="Explicit list of files (for testing/mocking)"
    )
    parser.add_argument("--env-file", default=None, help="Path to write changes.env")
    parser.add_argument("--json", action="store_true", help="Print JSON output to stdout")
    args = parser.parse_args(argv)

    if args.state_in is not None:
        prev_fps = load_state(args.state_in)
        current_fps = compute_fingerprints(args.root)

        print("=== CI CONTENT FINGERPRINTS ===")
        for subsys in ["retrieval", "corpus", "terraform"]:
            cur = current_fps.get(subsys, "")[:12]
            prev = prev_fps.get(subsys, "")[:12] if prev_fps.get(subsys) else "<none>"
            chg = "CHANGED" if current_fps.get(subsys) != prev_fps.get(subsys) else "unchanged"
            print(f"  {subsys:10s}: prev={prev} cur={cur} ({chg})")

        retrieval_changed = current_fps.get("retrieval") != prev_fps.get("retrieval")
        corpus_changed = current_fps.get("corpus") != prev_fps.get("corpus")
        terraform_changed = current_fps.get("terraform") != prev_fps.get("terraform")

        flags = {
            "TESTS_ALWAYS_RUN": True,
            "RETRIEVAL_CHANGED": retrieval_changed,
            "TERRAFORM_CHANGED": terraform_changed,
            "CORPUS_CHANGED": corpus_changed,
            "ROUTER_S1_CHANGED": False,
        }

        if args.keep_prev:
            for subsys in args.keep_prev:
                if subsys in prev_fps:
                    current_fps[subsys] = prev_fps[subsys]
                else:
                    current_fps.pop(subsys, None)
                print(f"  Preserved previous fingerprint for '{subsys}' in state output.")

        if args.state_out:
            write_state(current_fps, args.state_out)
            print(f"Recorded CI state to: {args.state_out}")

    else:
        if args.files is not None:
            changed_files = args.files
        else:
            changed_files = get_changed_files_from_git(args.base_ref)
        flags = resolve_flags(changed_files)

        if args.state_out:
            current_fps = compute_fingerprints(args.root)
            write_state(current_fps, args.state_out)

    print("=== CHANGE-AWARE SUBSYSTEM FLAGS ===")
    for k, v in sorted(flags.items()):
        print(f"  {k} = {v}")

    if args.env_file:
        env_path = Path(args.env_file)
        write_env_file(flags, env_path)
        print(f"Subsystem flags written to: {env_path}")

    if args.json:
        print(json.dumps(flags, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
