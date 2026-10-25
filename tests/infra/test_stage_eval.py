"""test_stage_eval.py — Unit and Invariant Tests for Staging DEV-Only Eval Data.

Validates that:
- stage_eval_data rejects test queries (t-prefixed) and TEST split rows.
- Multi-annotator labels A, B, C2 are strictly filtered to DEV qids.
- Invariant: Zero leakage of test data into staged CI/CD evaluation artifacts.
- scripts/stage_eval_data.sh is executable and enforces contracts.

References:
- REQ-CICD-004: Nightly Retrieval Health and Drift Evaluation
"""

import json
from pathlib import Path
import pytest

from scripts.stage_eval_data import (
    filter_dev_only,
    stage_eval_data,
    validate_record_is_dev_only,
    verify_staged_directory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# REQ-CICD-004
def test_validate_record_is_dev_only_rejects_test_data():
    """Assert validate_record_is_dev_only rejects t-prefixed qids and split=test."""
    # Valid DEV record
    validate_record_is_dev_only({"qid": "d001", "split": "dev", "text": "Valid dev query"})

    # Test qid: t-prefixed
    with pytest.raises(ValueError, match="LEAKAGE DETECTED.*Query ID 't001' is a test query"):
        validate_record_is_dev_only({"qid": "t001", "split": "dev"})

    with pytest.raises(ValueError, match="LEAKAGE DETECTED.*Query ID 't045' is a test query"):
        validate_record_is_dev_only({"qid": "t045", "query": "where is package?"})

    # Test split
    with pytest.raises(ValueError, match="LEAKAGE DETECTED.*Record contains split='test'"):
        validate_record_is_dev_only({"qid": "d002", "split": "test"})


# REQ-CICD-004
def test_filter_dev_only_purges_test_records():
    """filter_dev_only must strictly discard any test qids or split=test records."""
    mixed = [
        {"qid": "d001", "split": "dev", "primary": "doc-1"},
        {"qid": "t001", "split": "test", "primary": "doc-2"},
        {"qid": "d002", "split": "dev", "primary": "doc-3"},
        {"qid": "t002", "split": "dev", "primary": "doc-4"},  # t-prefixed
        {"qid": "d003", "split": "test", "primary": "doc-5"},  # test split
    ]

    filtered = filter_dev_only(mixed)
    assert len(filtered) == 2
    assert [r["qid"] for r in filtered] == ["d001", "d002"]

    # Invariant: No t-prefixed qid or test split in output
    for r in filtered:
        assert not r["qid"].startswith("t")
        assert r["qid"].startswith("d")
        assert r.get("split") != "test"


# REQ-CICD-004
def test_stage_eval_data_end_to_end_zero_leakage(tmp_path):
    """End-to-end staging must produce valid files with 100% DEV and 0% TEST data."""
    # Mock DEV queries (with only d-prefixed qids)
    dev_queries_file = tmp_path / "dev.jsonl"
    dev_queries_file.write_text(
        json.dumps({"qid": "d001", "query": "how do I return an item?", "split": "dev"}) + "\n"
        + json.dumps({"qid": "d002", "query": "driver was rude", "split": "dev"}) + "\n"
    )

    # Mock labels containing both DEV and TEST rows
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    (labels_dir / "labels_A.jsonl").write_text(
        json.dumps({"qid": "d001", "primary": "faq-returns"}) + "\n"
        + json.dumps({"qid": "t001", "primary": "faq-test-leak"}) + "\n"
    )
    (labels_dir / "labels_B.jsonl").write_text(
        json.dumps({"qid": "d002", "primary": "faq-delivery"}) + "\n"
        + json.dumps({"qid": "t002", "primary": "faq-test-leak2"}) + "\n"
    )
    (labels_dir / "labels_C2.jsonl").write_text(
        json.dumps({"qid": "d001", "primary": "faq-returns", "relevant": ["faq-returns"]}) + "\n"
        + json.dumps({"qid": "d002", "primary": "faq-delivery", "relevant": ["faq-delivery"]}) + "\n"
        + json.dumps({"qid": "t001", "primary": "faq-test-leak", "relevant": ["faq-test-leak"]}) + "\n"
    )

    # Mock gold map
    gold_map_file = tmp_path / "gold_map.json"
    gold_map_file.write_text(json.dumps({"faq-old": "faq-new"}))

    staged_dir = tmp_path / "staged"
    counts = stage_eval_data(
        dev_queries_path=dev_queries_file,
        labels_dir=labels_dir,
        gold_map_path=gold_map_file,
        out_dir=staged_dir,
    )

    assert counts["dev_queries"] == 2
    assert counts["labels_a.jsonl"] == 1
    assert counts["labels_b.jsonl"] == 1
    assert counts["labels_c2.jsonl"] == 2

    # Invariant verification across all staged jsonl files
    verify_staged_directory(staged_dir)

    for jsonl_file in staged_dir.glob("*.jsonl"):
        lines = [json.loads(l) for l in jsonl_file.read_text().splitlines() if l.strip()]
        for rec in lines:
            qid = str(rec.get("qid") or rec.get("query_id") or "")
            assert not qid.startswith("t"), f"Found forbidden t-prefixed qid in {jsonl_file.name}: {qid}"
            assert qid.startswith("d"), f"Expected d-prefixed qid in {jsonl_file.name}: {qid}"
            assert rec.get("split") != "test", f"Found split='test' in {jsonl_file.name}"


# REQ-CICD-004
def test_stage_eval_rejects_staging_if_dev_queries_contain_test_qid(tmp_path):
    """If input dev_queries accidentally contains a test qid, staging MUST abort with ValueError."""
    bad_dev_queries = tmp_path / "bad_dev.jsonl"
    bad_dev_queries.write_text(
        json.dumps({"qid": "d001", "query": "dev query"}) + "\n"
        + json.dumps({"qid": "t001", "query": "leaked test query!"}) + "\n"
    )
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    gold_map_file = tmp_path / "gold_map.json"
    gold_map_file.write_text("{}")

    with pytest.raises(ValueError, match="LEAKAGE DETECTED.*Query ID 't001' is a test query"):
        stage_eval_data(
            dev_queries_path=bad_dev_queries,
            labels_dir=labels_dir,
            gold_map_path=gold_map_file,
            out_dir=tmp_path / "staged",
        )


# REQ-CICD-004
def test_stage_eval_shell_script_executable():
    """Assert scripts/stage_eval_data.sh exists and is executable."""
    sh_script = REPO_ROOT / "scripts" / "stage_eval_data.sh"
    assert sh_script.is_file(), f"Missing {sh_script}"
    import os
    assert os.access(sh_script, os.X_OK), f"{sh_script} is not executable"
