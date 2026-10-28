"""Unit tests for held-out evaluation pre-registration and single-use guards.

# REQ-REVAL-010: Held-out pre-registration & single use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.routing.heldout import (
    check_allowed,
    preregister,
    record_result,
)


def test_heldout_preregister_and_check_allowed_flow(tmp_path: Path):
    """# REQ-REVAL-010: Happy path for held-out preregistration and execution verification."""
    log_path = tmp_path / "heldout_registry.log"
    version = "routing_v1.0.0"
    manifest_sha = "abc123sha"
    cfg_hash = "cfg_hash_dev_001"
    gates = {"strict_safety_recall_min": 0.98}

    prereg_id = preregister(
        dataset_version=version,
        manifest_sha256=manifest_sha,
        config_hash=cfg_hash,
        gate_thresholds=gates,
        operator="susmith",
        log_path=log_path,
    )

    assert prereg_id.startswith("prereg-")
    assert log_path.exists()

    # Allowed when dev run completed
    rec = check_allowed(
        prereg_id=prereg_id,
        dataset_version=version,
        manifest_sha=manifest_sha,
        config_hash=cfg_hash,
        completed_dev_config_hashes={cfg_hash},
        log_path=log_path,
    )
    assert rec["prereg_id"] == prereg_id
    assert rec["status"] == "registered"

    # Record execution results
    record_result(
        prereg_id=prereg_id,
        results_summary={"accuracy": 0.95, "recall": 0.99},
        log_path=log_path,
    )

    # Second execution attempt with same prereg_id must be blocked
    with pytest.raises(PermissionError, match="already been executed"):
        check_allowed(
            prereg_id=prereg_id,
            dataset_version=version,
            manifest_sha=manifest_sha,
            config_hash=cfg_hash,
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )


def test_heldout_check_allowed_mismatch_guards(tmp_path: Path):
    """# REQ-REVAL-010: check_allowed rejects missing, mismatched, or untested configs."""
    log_path = tmp_path / "heldout_registry.log"
    version = "routing_v1.0.0"
    manifest_sha = "abc123sha"
    cfg_hash = "cfg_hash_dev_001"

    prereg_id = preregister(
        dataset_version=version,
        manifest_sha256=manifest_sha,
        config_hash=cfg_hash,
        gate_thresholds={"strict_safety_recall_min": 0.98},
        operator="susmith",
        log_path=log_path,
    )

    # 1. Unregistered ID
    with pytest.raises(KeyError, match="not found in registry"):
        check_allowed(
            prereg_id="prereg-unknown-id",
            dataset_version=version,
            manifest_sha=manifest_sha,
            config_hash=cfg_hash,
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )

    # 2. Version mismatch
    with pytest.raises(ValueError, match="Dataset version mismatch"):
        check_allowed(
            prereg_id=prereg_id,
            dataset_version="routing_v2.0.0",
            manifest_sha=manifest_sha,
            config_hash=cfg_hash,
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )

    # 3. Manifest sha mismatch
    with pytest.raises(ValueError, match="Manifest SHA256 mismatch"):
        check_allowed(
            prereg_id=prereg_id,
            dataset_version=version,
            manifest_sha="different_manifest_sha",
            config_hash=cfg_hash,
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )

    # 4. Config hash mismatch
    with pytest.raises(ValueError, match="Config hash mismatch"):
        check_allowed(
            prereg_id=prereg_id,
            dataset_version=version,
            manifest_sha=manifest_sha,
            config_hash="different_config_hash",
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )

    # 5. Untested on dev split
    with pytest.raises(PermissionError, match="not produced by a completed dev run"):
        check_allowed(
            prereg_id=prereg_id,
            dataset_version=version,
            manifest_sha=manifest_sha,
            config_hash=cfg_hash,
            completed_dev_config_hashes={"other_dev_hash"},
            log_path=log_path,
        )


def test_heldout_single_use_per_version_config(tmp_path: Path):
    """# REQ-REVAL-010: Forbids tuning on heldout data even if a second preregistration is created."""
    log_path = tmp_path / "heldout_registry.log"
    version = "routing_v1.0.0"
    manifest_sha = "abc123sha"
    cfg_hash = "cfg_hash_dev_001"

    # Run 1
    prereg_1 = preregister(
        dataset_version=version,
        manifest_sha256=manifest_sha,
        config_hash=cfg_hash,
        gate_thresholds={},
        operator="susmith",
        log_path=log_path,
    )
    check_allowed(
        prereg_id=prereg_1,
        dataset_version=version,
        manifest_sha=manifest_sha,
        config_hash=cfg_hash,
        completed_dev_config_hashes={cfg_hash},
        log_path=log_path,
    )
    record_result(prereg_id=prereg_1, results_summary={"recall": 0.99}, log_path=log_path)

    # Attempt to preregister and run the same (version, config) a second time
    prereg_2 = preregister(
        dataset_version=version,
        manifest_sha256=manifest_sha,
        config_hash=cfg_hash,
        gate_thresholds={},
        operator="susmith",
        log_path=log_path,
    )
    with pytest.raises(PermissionError, match="already completed for dataset version"):
        check_allowed(
            prereg_id=prereg_2,
            dataset_version=version,
            manifest_sha=manifest_sha,
            config_hash=cfg_hash,
            completed_dev_config_hashes={cfg_hash},
            log_path=log_path,
        )
