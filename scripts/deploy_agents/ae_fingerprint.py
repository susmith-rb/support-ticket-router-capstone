#!/usr/bin/env python3
"""Change-aware Agent Engine update fingerprint calculator and deploy gating.

# REQ-CD-011: Change-aware Agent Engine update
Computes deterministic SHA-256 fingerprint of the TriageRouter package, its
deploy script, and pyproject.toml dependencies to skip Agent Engine rollout
when source files and build configurations are unchanged.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# =============================================================================
# REQ-CD-011: Inclusion and Exclusion Specifications
# =============================================================================
INCLUDE_DIRS: tuple[str, ...] = ("src/ticket_router",)
INCLUDE_FILES: tuple[str, ...] = (
    "scripts/deploy_agents/deploy_triage_agent_runtime.py",
    "pyproject.toml",
)
EXCLUDE_SUBPATHS: tuple[str, ...] = (
    "agents/faq_resolver",
    "agents/human_queue",
    "mcp",
    "store",
    "retrieval",
    "serve.py",
)
EXCLUDE_DIR_NAMES: tuple[str, ...] = ("__pycache__",)
EXCLUDE_FILENAME_SUFFIXES: tuple[str, ...] = (".pyc",)

GCS_RECORD_PATH: str = "cd/agent_engine_deployed.json"


def _clean_bucket_name(bucket: str) -> str:
    """Strip protocol and trailing paths from bucket argument."""
    cleaned = bucket.strip()
    if cleaned.startswith("gs://"):
        cleaned = cleaned[len("gs://") :]
    return cleaned.split("/")[0]


def _parse_bool(val: str | bool | None) -> bool:
    """Parse string or boolean flag value."""
    if isinstance(val, bool):
        return val
    if not val:
        return False
    return val.strip().lower() in ("true", "1", "yes", "y", "t")


def find_fingerprint_files(root: Path | str = ".") -> list[tuple[str, Path]]:
    """Collect all files contributing to the TriageRouter Agent Engine fingerprint.

    Returns a deterministically sorted list of tuples (posix_relative_path, absolute_file_path).
    """
    root_path = Path(root).resolve()
    collected: dict[str, Path] = {}

    for rel_dir in INCLUDE_DIRS:
        dir_path = root_path / rel_dir
        if not dir_path.is_dir():
            continue
        for path in dir_path.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
                continue
            if any(path.name.endswith(suffix) for suffix in EXCLUDE_FILENAME_SUFFIXES):
                continue

            sub = path.relative_to(dir_path).as_posix()
            if any(sub == excl or sub.startswith(f"{excl}/") for excl in EXCLUDE_SUBPATHS):
                continue

            rel_posix = path.relative_to(root_path).as_posix()
            collected[rel_posix] = path

    for rel_file in INCLUDE_FILES:
        file_path = root_path / rel_file
        if file_path.is_file() and not file_path.is_symlink():
            rel_posix = file_path.relative_to(root_path).as_posix()
            collected[rel_posix] = file_path

    return sorted(collected.items(), key=lambda item: item[0])


def compute_fingerprint(root: Path | str = ".") -> str:
    """Compute hex SHA-256 over sorted (posix relative path + NUL + file bytes + NUL)."""
    hasher = hashlib.sha256()
    for rel_posix, abs_path in find_fingerprint_files(root):
        content = abs_path.read_bytes()
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(content)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def get_gcs_client() -> Any:
    """Lazily instantiate and return google.cloud.storage.Client."""
    from google.cloud import storage  # Lazy import for dev-only environments

    return storage.Client()


def get_deployed_record(bucket_name: str, storage_client: Any = None) -> dict[str, Any]:
    """Retrieve parsed agent_engine_deployed.json dict from GCS."""
    client = storage_client if storage_client is not None else get_gcs_client()
    b = client.bucket(_clean_bucket_name(bucket_name))
    blob = b.blob(GCS_RECORD_PATH)
    data_str = blob.download_as_text(encoding="utf-8")
    parsed = json.loads(data_str)
    if not isinstance(parsed, dict):
        raise ValueError("Deployed record is not a valid JSON object")
    return parsed


def write_deployed_record(
    bucket_name: str,
    record: dict[str, Any],
    storage_client: Any = None,
) -> None:
    """Write agent_engine_deployed.json to GCS."""
    client = storage_client if storage_client is not None else get_gcs_client()
    b = client.bucket(_clean_bucket_name(bucket_name))
    blob = b.blob(GCS_RECORD_PATH)
    blob.upload_from_string(
        json.dumps(record, indent=2) + "\n",
        content_type="application/json",
    )


def should_deploy(
    bucket_name: str,
    force: bool = False,
    root: Path | str = ".",
    storage_client: Any = None,
) -> tuple[int, str]:
    """Evaluate whether Agent Engine update should proceed or be skipped.

    Returns (exit_code, reason_message).
    - exit 0: DEPLOY
    - exit 3: SKIP
    """
    if force:
        return 0, "DEPLOY: force requested (_FORCE_AGENT_ENGINE=true)"

    current_fp = compute_fingerprint(root)

    try:
        data = get_deployed_record(bucket_name, storage_client=storage_client)
    except Exception as exc:
        return 0, f"DEPLOY: deployed record missing or unreadable ({exc})"

    if "fingerprint" not in data:
        return 0, "DEPLOY: deployed record malformed (missing 'fingerprint' field)"

    deployed_fp = str(data["fingerprint"])
    deployed_sha = str(data.get("sha", "unknown"))

    if deployed_fp != current_fp:
        return 0, f"DEPLOY: fingerprint changed (deployed: {deployed_fp}, current: {current_fp})"

    return 3, f"SKIP: fingerprint {current_fp} matches deployed sha {deployed_sha}"


def record_deployed(
    bucket_name: str,
    sha: str,
    root: Path | str = ".",
    storage_client: Any = None,
) -> dict[str, Any]:
    """Record successful deployment state to GCS."""
    fp = compute_fingerprint(root)
    record = {
        "fingerprint": fp,
        "sha": sha,
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    write_deployed_record(bucket_name, record, storage_client=storage_client)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Engine change detection & fingerprinting")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    compute_p = subparsers.add_parser("compute", help="Compute current code fingerprint")
    compute_p.add_argument("--root", default=".", help="Repository root path")

    should_p = subparsers.add_parser("should-deploy", help="Check if deployment should proceed")
    should_p.add_argument("--bucket", required=True, help="GCS bucket name holding deployed state")
    should_p.add_argument("--force", default="false", help="Force deployment (true/false)")
    should_p.add_argument("--root", default=".", help="Repository root path")

    record_p = subparsers.add_parser("record", help="Record deployed fingerprint to GCS")
    record_p.add_argument("--bucket", required=True, help="GCS bucket name")
    record_p.add_argument("--sha", required=True, help="Git commit SHA deployed")
    record_p.add_argument("--root", default=".", help="Repository root path")

    args = parser.parse_args(argv)

    if args.subcommand == "compute":
        print(compute_fingerprint(args.root))
        return 0

    if args.subcommand == "should-deploy":
        rc, msg = should_deploy(
            bucket_name=args.bucket,
            force=_parse_bool(args.force),
            root=args.root,
        )
        print(msg)
        return rc

    if args.subcommand == "record":
        rec = record_deployed(
            bucket_name=args.bucket,
            sha=args.sha,
            root=args.root,
        )
        print(
            f"RECORDED: gs://{_clean_bucket_name(args.bucket)}/{GCS_RECORD_PATH} "
            f"(fingerprint={rec['fingerprint']}, sha={rec['sha']})"
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
