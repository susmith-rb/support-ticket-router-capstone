# tests/infra/test_ae_fingerprint.py
# Verification and unit tests for change-aware Agent Engine fingerprinting.
# Implements REQ-CD-011.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.deploy_agents.ae_fingerprint import (
    compute_fingerprint,
    main,
    record_deployed,
    should_deploy,
)


class FakeBlob:
    """In-memory blob fake for hermetic GCS testing."""

    def __init__(self, content: str | bytes | None = None, should_fail: bool = False):
        self.content = content
        self.should_fail = should_fail
        self.uploaded_data: str | None = None
        self.uploaded_content_type: str | None = None

    def download_as_text(self, encoding: str = "utf-8") -> str:
        if self.should_fail:
            raise RuntimeError("Simulated GCS network / permission failure")
        if self.content is None:
            raise FileNotFoundError("Blob gs://test-bucket/cd/agent_engine_deployed.json not found")
        if isinstance(self.content, bytes):
            return self.content.decode(encoding)
        return self.content

    def upload_from_string(self, data: str | bytes, content_type: str | None = None) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated GCS upload failure")
        self.uploaded_data = data.decode("utf-8") if isinstance(data, bytes) else data
        self.uploaded_content_type = content_type


class FakeBucket:
    def __init__(self, blob: FakeBlob):
        self._blob = blob

    def blob(self, path: str) -> FakeBlob:
        return self._blob


class FakeStorageClient:
    def __init__(self, blob: FakeBlob | None = None):
        self.blob = blob or FakeBlob()

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.blob)


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """Construct minimal reproducible package tree matching REQ-CD-011 boundaries."""
    # Included components
    (tmp_path / "src" / "ticket_router" / "agents" / "triage_router").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "agents" / "triage_router" / "agent.py").write_text(
        "class TriageRouter:\n    pass\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "common").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "common" / "config.py").write_text(
        "CONFIG_KEY = 'val'\n", encoding="utf-8"
    )

    (tmp_path / "scripts" / "deploy_agents").mkdir(parents=True)
    (tmp_path / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py").write_text(
        "# deploy triage script\n", encoding="utf-8"
    )

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'support-ticket-router'\n", encoding="utf-8"
    )

    (tmp_path / "prompts").mkdir(parents=True)
    (tmp_path / "prompts" / "tier2_router_v3.yaml").write_text(
        "version: v3\n", encoding="utf-8"
    )
    (tmp_path / "prompts" / "faq_answer_v1.yaml").write_text(
        "version: v1\n", encoding="utf-8"
    )

    # Excluded components
    (tmp_path / "src" / "ticket_router" / "agents" / "faq_resolver").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "agents" / "faq_resolver" / "faq.py").write_text(
        "# faq\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "agents" / "human_queue").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "agents" / "human_queue" / "queue.py").write_text(
        "# queue\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "mcp").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "mcp" / "server.py").write_text(
        "# mcp\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "store").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "store" / "db.py").write_text(
        "# store\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "retrieval").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "retrieval" / "client.py").write_text(
        "# retrieval\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "serve.py").write_text(
        "# serve entrypoint\n", encoding="utf-8"
    )

    (tmp_path / "src" / "ticket_router" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "ticket_router" / "__pycache__" / "agent.cpython-311.pyc").write_bytes(
        b"\x00\x01\x02\x03"
    )

    (tmp_path / "src" / "ticket_router" / "agents" / "triage_router" / "extra.pyc").write_bytes(
        b"\x04\x05\x06"
    )

    return tmp_path


# REQ-CD-011
def test_fingerprint_change_detection(sample_tree: Path) -> None:
    """Verify change-aware fingerprint inclusion and exclusion boundaries.

    Invariants:
    1. Changes to triage_router/*.py, common/*.py, pyproject.toml, or deploy script change fingerprint.
    2. Changes to faq_resolver, human_queue, mcp, store, retrieval, serve.py, __pycache__, or *.pyc DO NOT change fingerprint.
    """
    base_fp = compute_fingerprint(sample_tree)
    assert len(base_fp) == 64
    assert isinstance(base_fp, str)

    # 1. POSITIVE CASES: Included files change the fingerprint
    # Modifying triage_router
    triage_file = sample_tree / "src" / "ticket_router" / "agents" / "triage_router" / "agent.py"
    original_triage = triage_file.read_text(encoding="utf-8")
    triage_file.write_text(original_triage + "\n# modification\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) != base_fp
    triage_file.write_text(original_triage, encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying common
    common_file = sample_tree / "src" / "ticket_router" / "common" / "config.py"
    original_common = common_file.read_text(encoding="utf-8")
    common_file.write_text(original_common + "\n# modification\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) != base_fp
    common_file.write_text(original_common, encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying pyproject.toml
    pyproject = sample_tree / "pyproject.toml"
    original_pyproject = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(original_pyproject + "\n# dep bump\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) != base_fp
    pyproject.write_text(original_pyproject, encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying deploy script
    deploy_script = sample_tree / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py"
    original_deploy = deploy_script.read_text(encoding="utf-8")
    deploy_script.write_text(original_deploy + "\n# runtime change\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) != base_fp
    deploy_script.write_text(original_deploy, encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying prompts/tier2_router_v3.yaml
    tier2_prompt_file = sample_tree / "prompts" / "tier2_router_v3.yaml"
    original_tier2_prompt = tier2_prompt_file.read_text(encoding="utf-8")
    tier2_prompt_file.write_text(original_tier2_prompt + "\n# prompt tweak\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) != base_fp
    tier2_prompt_file.write_text(original_tier2_prompt, encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # 2. NEGATIVE CASES: Excluded components DO NOT change the fingerprint
    # Modifying prompts/faq_answer_v1.yaml
    faq_prompt_file = sample_tree / "prompts" / "faq_answer_v1.yaml"
    faq_prompt_file.write_text("faq prompt modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp
    # Modifying faq_resolver
    faq_file = sample_tree / "src" / "ticket_router" / "agents" / "faq_resolver" / "faq.py"
    faq_file.write_text("faq modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying human_queue
    queue_file = sample_tree / "src" / "ticket_router" / "agents" / "human_queue" / "queue.py"
    queue_file.write_text("queue modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying mcp
    mcp_file = sample_tree / "src" / "ticket_router" / "mcp" / "server.py"
    mcp_file.write_text("mcp modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying store
    store_file = sample_tree / "src" / "ticket_router" / "store" / "db.py"
    store_file.write_text("store modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying retrieval
    retrieval_file = sample_tree / "src" / "ticket_router" / "retrieval" / "client.py"
    retrieval_file.write_text("retrieval modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying serve.py
    serve_file = sample_tree / "src" / "ticket_router" / "serve.py"
    serve_file.write_text("serve modified\n", encoding="utf-8")
    assert compute_fingerprint(sample_tree) == base_fp

    # Modifying __pycache__ or *.pyc
    pyc_file1 = sample_tree / "src" / "ticket_router" / "__pycache__" / "agent.cpython-311.pyc"
    pyc_file1.write_bytes(b"\xff\xfe\xfd")
    assert compute_fingerprint(sample_tree) == base_fp

    pyc_file2 = sample_tree / "src" / "ticket_router" / "agents" / "triage_router" / "extra.pyc"
    pyc_file2.write_bytes(b"\x99\x88\x77")
    assert compute_fingerprint(sample_tree) == base_fp


# REQ-CD-011
def test_fingerprint_ordering_deterministic(tmp_path: Path) -> None:
    """Verify fingerprint calculation is invariant to directory iteration or file creation order."""
    dir_a = tmp_path / "tree_a"
    dir_b = tmp_path / "tree_b"

    for d in (dir_a, dir_b):
        (d / "src" / "ticket_router" / "agents" / "triage_router").mkdir(parents=True)
        (d / "scripts" / "deploy_agents").mkdir(parents=True)

    # In tree A, write triage first, then pyproject, then deploy script
    (dir_a / "src" / "ticket_router" / "agents" / "triage_router" / "agent.py").write_text("a=1\n")
    (dir_a / "pyproject.toml").write_text("[project]\nname='x'\n")
    (dir_a / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py").write_text("deploy\n")

    # In tree B, write deploy script first, then pyproject, then triage
    (dir_b / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py").write_text("deploy\n")
    (dir_b / "pyproject.toml").write_text("[project]\nname='x'\n")
    (dir_b / "src" / "ticket_router" / "agents" / "triage_router" / "agent.py").write_text("a=1\n")

    assert compute_fingerprint(dir_a) == compute_fingerprint(dir_b)


# REQ-CD-011
def test_should_deploy_decisions(sample_tree: Path) -> None:
    """Verify should-deploy exit codes and reasoning under all contract conditions.

    Invariants:
    1. match -> exit 3 (SKIP)
    2. mismatch -> exit 0 (DEPLOY)
    3. missing -> exit 0 (DEPLOY, fail-safe)
    4. error -> exit 0 (DEPLOY, fail-safe)
    5. force -> exit 0 (DEPLOY)
    6. malformed -> exit 0 (DEPLOY, fail-safe)
    """
    current_fp = compute_fingerprint(sample_tree)

    # 1. Match -> 3 (SKIP)
    match_payload = json.dumps({"fingerprint": current_fp, "sha": "commit-1234", "time": "2026-10-28T00:00:00Z"})
    client_match = FakeStorageClient(FakeBlob(content=match_payload))
    rc, msg = should_deploy("test-bucket", force=False, root=sample_tree, storage_client=client_match)
    assert rc == 3
    assert f"SKIP: fingerprint {current_fp} matches deployed sha commit-1234" in msg

    # 2. Mismatch -> 0 (DEPLOY)
    mismatch_payload = json.dumps({"fingerprint": "different-fp-0000", "sha": "commit-old", "time": "2026-10-28T00:00:00Z"})
    client_mismatch = FakeStorageClient(FakeBlob(content=mismatch_payload))
    rc, msg = should_deploy("test-bucket", force=False, root=sample_tree, storage_client=client_mismatch)
    assert rc == 0
    assert "DEPLOY: fingerprint changed" in msg
    assert "different-fp-0000" in msg

    # 3. Missing record -> 0 (DEPLOY)
    client_missing = FakeStorageClient(FakeBlob(content=None))
    rc, msg = should_deploy("test-bucket", force=False, root=sample_tree, storage_client=client_missing)
    assert rc == 0
    assert "DEPLOY: deployed record missing or unreadable" in msg

    # 4. GCS error -> 0 (DEPLOY)
    client_error = FakeStorageClient(FakeBlob(should_fail=True))
    rc, msg = should_deploy("test-bucket", force=False, root=sample_tree, storage_client=client_error)
    assert rc == 0
    assert "DEPLOY: deployed record missing or unreadable" in msg

    # 5. Force requested -> 0 (DEPLOY)
    rc, msg = should_deploy("test-bucket", force=True, root=sample_tree, storage_client=client_match)
    assert rc == 0
    assert "DEPLOY: force requested" in msg

    # 6. Malformed JSON -> 0 (DEPLOY)
    client_malformed = FakeStorageClient(FakeBlob(content=json.dumps({"some_key": "no_fingerprint"})))
    rc, msg = should_deploy("test-bucket", force=False, root=sample_tree, storage_client=client_malformed)
    assert rc == 0
    assert "DEPLOY: deployed record malformed" in msg


# REQ-CD-011
def test_record_writes_json(sample_tree: Path) -> None:
    """Verify record_deployed writes agent_engine_deployed.json with required schema."""
    fake_client = FakeStorageClient(FakeBlob())
    rec = record_deployed(
        bucket_name="susmith-fde-ticket-router-laya",
        sha="a1b2c3d4",
        root=sample_tree,
        storage_client=fake_client,
    )

    expected_fp = compute_fingerprint(sample_tree)
    assert rec["fingerprint"] == expected_fp
    assert rec["sha"] == "a1b2c3d4"
    assert "time" in rec

    uploaded_str = fake_client.blob.uploaded_data
    assert uploaded_str is not None
    data = json.loads(uploaded_str)
    assert data["fingerprint"] == expected_fp
    assert data["sha"] == "a1b2c3d4"
    assert data["time"] == rec["time"]
    assert fake_client.blob.uploaded_content_type == "application/json"


# REQ-CD-011
def test_cli_subcommands(sample_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI main() routing, flags, exit codes, and stdout output."""
    # Test compute
    rc = main(["compute", "--root", str(sample_tree)])
    assert rc == 0
    captured = capsys.readouterr()
    expected_fp = compute_fingerprint(sample_tree)
    assert captured.out.strip() == expected_fp

    # Test should-deploy (monkeypatch get_deployed_record)
    def fake_get_record(bucket: str, storage_client: Any = None):
        return {"fingerprint": expected_fp, "sha": "deployed-sha"}

    monkeypatch.setattr("scripts.deploy_agents.ae_fingerprint.get_deployed_record", fake_get_record)
    rc_skip = main(["should-deploy", "--bucket", "test-bkt", "--root", str(sample_tree)])
    assert rc_skip == 3
    captured_skip = capsys.readouterr()
    assert "SKIP:" in captured_skip.out

    # Test force
    rc_force = main(["should-deploy", "--bucket", "test-bkt", "--force", "true", "--root", str(sample_tree)])
    assert rc_force == 0
    captured_force = capsys.readouterr()
    assert "DEPLOY: force requested" in captured_force.out

    # Test record (monkeypatch write_deployed_record)
    recorded_records = []

    def fake_write_record(bucket: str, record: dict[str, Any], storage_client: Any = None):
        recorded_records.append(record)

    monkeypatch.setattr("scripts.deploy_agents.ae_fingerprint.write_deployed_record", fake_write_record)
    rc_rec = main(["record", "--bucket", "test-bkt", "--sha", "feedbeef", "--root", str(sample_tree)])
    assert rc_rec == 0
    captured_rec = capsys.readouterr()
    assert "RECORDED:" in captured_rec.out
    assert len(recorded_records) == 1
    assert recorded_records[0]["sha"] == "feedbeef"
    assert recorded_records[0]["fingerprint"] == expected_fp
