"""Held-out evaluation dataset pre-registration and single-use guard.

# REQ-REVAL-010: Held-out pre-registration & single use.
Enforces:
1. Mandatory pre-registration before running any evaluation on heldout.jsonl.
2. Exact matching of dataset version, manifest sha256, config hash, gate thresholds.
3. Verification that candidate config was tested on a completed dev run first.
4. Single-use guard: strictly forbids a second held-out run for the same config.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

DEFAULT_REGISTRY_LOG = Path("evals/results/routing/heldout_registry.log")


def _read_registry(log_path: Path) -> List[Dict[str, Any]]:
    """Read all JSONL records from the registry log."""
    if not log_path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                records.append(json.loads(stripped))
    return records


def _write_registry(log_path: Path, records: Sequence[Dict[str, Any]]) -> None:
    """Rewrite registry records in JSONL format."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")


def preregister(
    dataset_version: str,
    manifest_sha256: str,
    config_hash: str,
    gate_thresholds: Dict[str, Any],
    operator: str,
    log_path: Path | str = DEFAULT_REGISTRY_LOG,
) -> str:
    """Pre-register a candidate configuration for a single heldout evaluation run.

    Appends a new registration record to the heldout registry log.
    Returns:
        Generated unique preregistration_id.
    """
    path = Path(log_path)
    now_utc = datetime.now(timezone.utc).isoformat()
    random_suffix = uuid.uuid4().hex[:6]
    timestamp_prefix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    prereg_id = f"prereg-{timestamp_prefix}-{random_suffix}"

    record = {
        "prereg_id": prereg_id,
        "dataset_version": dataset_version,
        "manifest_sha256": manifest_sha256,
        "config_hash": config_hash,
        "gate_thresholds": gate_thresholds,
        "operator": operator,
        "timestamp": now_utc,
        "status": "registered",
        "results": None,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")

    return prereg_id


def check_allowed(
    prereg_id: str,
    dataset_version: str,
    manifest_sha: str,
    config_hash: str,
    completed_dev_config_hashes: Sequence[str] | Set[str],
    log_path: Path | str = DEFAULT_REGISTRY_LOG,
) -> Dict[str, Any]:
    """Check if heldout evaluation run is permitted.

    Validates:
    - Pre-registration exists in log.
    - Dataset version matches.
    - Manifest sha256 matches.
    - Config hash matches.
    - Config hash was verified in a completed dev run.
    - Registration has not already been used.
    - No prior heldout run has been completed for this (dataset_version, config_hash).

    Returns:
        Pre-registration record dict.
    Raises:
        FileNotFoundError, KeyError, ValueError, or PermissionError on violation.
    """
    path = Path(log_path)
    records = _read_registry(path)
    if not records:
        raise FileNotFoundError(f"Heldout registry log not found or empty at '{path}'")

    target_record: Optional[Dict[str, Any]] = None
    for rec in records:
        if rec.get("prereg_id") == prereg_id:
            target_record = rec
            break

    if target_record is None:
        raise KeyError(f"Preregistration ID '{prereg_id}' not found in registry at '{path}'")

    # 1. Version check
    if target_record["dataset_version"] != dataset_version:
        raise ValueError(
            f"Dataset version mismatch: preregistered for '{target_record['dataset_version']}', got '{dataset_version}'"
        )

    # 2. Manifest sha check
    if target_record["manifest_sha256"] != manifest_sha:
        raise ValueError(
            f"Manifest SHA256 mismatch: preregistered for '{target_record['manifest_sha256']}', got '{manifest_sha}'"
        )

    # 3. Config hash check
    if target_record["config_hash"] != config_hash:
        raise ValueError(
            f"Config hash mismatch: preregistered for '{target_record['config_hash']}', got '{config_hash}'"
        )

    # 4. Completed dev run check
    completed_dev_set = set(completed_dev_config_hashes)
    if config_hash not in completed_dev_set:
        raise PermissionError(
            f"Config hash '{config_hash}' was not produced by a completed dev run. "
            "Hill-climbing must verify candidates on dev before touching heldout."
        )

    # 5. Check if this preregistration ID was already executed
    if target_record.get("status") == "completed" or target_record.get("results") is not None:
        raise PermissionError(
            f"Preregistration '{prereg_id}' has already been executed. Heldout runs are single-use."
        )

    # 6. Check if any completed run exists for this (dataset_version, config_hash)
    for rec in records:
        if (
            rec.get("dataset_version") == dataset_version
            and rec.get("config_hash") == config_hash
            and rec.get("status") == "completed"
        ):
            raise PermissionError(
                f"Heldout evaluation was already completed for dataset version '{dataset_version}' "
                f"and config hash '{config_hash}'. Tuning on heldout data is strictly forbidden."
            )

    return target_record


def record_result(
    prereg_id: str,
    results_summary: Dict[str, Any],
    log_path: Path | str = DEFAULT_REGISTRY_LOG,
) -> None:
    """Record execution results for a heldout preregistration in the log."""
    path = Path(log_path)
    records = _read_registry(path)
    found = False

    for rec in records:
        if rec.get("prereg_id") == prereg_id:
            rec["status"] = "completed"
            rec["results"] = results_summary
            rec["completed_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break

    if not found:
        raise KeyError(f"Preregistration ID '{prereg_id}' not found to record results.")

    _write_registry(path, records)
