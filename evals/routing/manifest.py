"""Manifest generation and immutable version directory management.

# REQ-REVAL-009: Deterministic group-aware splits & dataset versioning.
Builds manifest metadata (counts by source, scenario, intent, label status;
git SHA; input/output SHA256s; coverage and dedup reports) and enforces
immutable version directory creation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from evals.routing.schema import RoutingCase, write_jsonl


def _get_git_sha() -> str:
    """Retrieve git HEAD SHA with graceful fallback."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _compute_file_sha256(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_split_breakdown(cases: Sequence[RoutingCase]) -> Dict[str, Any]:
    """Compute counts by source, scenario, intent, and label status."""
    by_source: Dict[str, int] = {}
    by_scenario: Dict[str, int] = {}
    by_intent: Dict[str, int] = {}
    by_label_status: Dict[str, int] = {}

    for c in cases:
        by_source[c.source] = by_source.get(c.source, 0) + 1
        for sc in c.scenarios or ():
            by_scenario[sc] = by_scenario.get(sc, 0) + 1
        by_intent[c.expected.intent] = by_intent.get(c.expected.intent, 0) + 1
        by_label_status[c.label_status] = by_label_status.get(c.label_status, 0) + 1

    return {
        "total": len(cases),
        "by_source": dict(sorted(by_source.items())),
        "by_scenario": dict(sorted(by_scenario.items())),
        "by_intent": dict(sorted(by_intent.items())),
        "by_label_status": dict(sorted(by_label_status.items())),
    }


def build_manifest(
    dataset_version: str,
    seed: int,
    splits: Dict[str, Sequence[RoutingCase]],
    git_sha: Optional[str] = None,
    input_sha256s: Optional[Dict[str, str]] = None,
    coverage_report: Optional[Dict[str, Any]] = None,
    dedup_report: Optional[Dict[str, Any]] = None,
    output_sha256s: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build manifest dictionary conforming to REQ-REVAL-009."""
    split_counts: Dict[str, Any] = {}
    for split_name in ("dev", "regression", "heldout", "pool"):
        cases = splits.get(split_name, [])
        split_counts[split_name] = compute_split_breakdown(cases)

    return {
        "dataset_version": dataset_version,
        "seed": seed,
        "git_sha": git_sha or _get_git_sha(),
        "input_sha256s": dict(sorted((input_sha256s or {}).items())),
        "split_counts": split_counts,
        "coverage_report": coverage_report or {},
        "dedup_report": dedup_report or {},
        "output_sha256s": dict(sorted((output_sha256s or {}).items())),
    }


def write_version_dir(
    version_dir: Path | str,
    splits: Dict[str, Sequence[RoutingCase]],
    manifest_data: Dict[str, Any],
    changelog_entry: Optional[str] = None,
) -> Path:
    """Write an immutable dataset version directory with splits and manifest.

    Refuses to overwrite an existing version directory.
    Writes:
    - dev.jsonl, regression.jsonl, heldout.jsonl, pool.jsonl
    - manifest.json
    - CHANGELOG.md

    Returns:
        Path to the created version directory.
    """
    vdir = Path(version_dir)
    if vdir.exists():
        raise FileExistsError(
            f"Version directory '{vdir}' already exists. Released dataset versions are strictly immutable."
        )

    vdir.mkdir(parents=True, exist_ok=False)

    output_sha256s: Dict[str, str] = {}
    for split_name in ("dev", "regression", "heldout", "pool"):
        cases = splits.get(split_name, [])
        file_path = vdir / f"{split_name}.jsonl"
        write_jsonl(file_path, cases)
        output_sha256s[f"{split_name}.jsonl"] = _compute_file_sha256(file_path)

    # Update manifest with output hashes and write manifest.json
    final_manifest = dict(manifest_data)
    final_manifest["output_sha256s"] = dict(sorted(output_sha256s.items()))

    manifest_path = vdir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(final_manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    # Write CHANGELOG.md
    changelog_path = vdir / "CHANGELOG.md"
    entry = changelog_entry or f"# Changelog for {final_manifest.get('dataset_version', 'version')}\n\n- Initial release.\n"
    changelog_path.write_text(entry, encoding="utf-8")

    return vdir
