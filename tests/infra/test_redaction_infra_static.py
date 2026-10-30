"""Static and contract tests for redaction map KMS envelope and purge infrastructure.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
# REQ-STORE-010: Redaction Map 30-Day TTL Purge
"""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
    InMemoryTicketDbMcpClient,
)
from ticket_router.mcp.ticket_db.server import create_app as create_ticket_db_app
from ticket_router.security.envelope import EnvelopeCipher, RedactionSealer
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import TicketCreate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
TF_MODULES_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project" / "modules"


class MockKmsSession:
    """Hermetic in-memory mock for KMS REST encrypt/decrypt calls."""

    def post(self, url: str, json: dict, headers: dict, timeout: float = 10.0):
        class Resp:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def json(self):
                return self._data

            def raise_for_status(self):
                pass

        payload = json or {}
        if url.endswith(":encrypt"):
            raw_dek = base64.b64decode(payload.get("plaintext", ""))
            return Resp({
                "ciphertext": base64.b64encode(b"MOCK_WRAP:" + raw_dek).decode("ascii"),
                "name": "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper/cryptoKeyVersions/1",
            })
        if url.endswith(":decrypt"):
            ct = base64.b64decode(payload.get("ciphertext", ""))
            assert ct.startswith(b"MOCK_WRAP:")
            return Resp({
                "plaintext": base64.b64encode(ct[len(b"MOCK_WRAP:"):]).decode("ascii"),
            })
        return Resp({})


# REQ-STORE-009
# REQ-STORE-010
def test_migration_0004_redaction_envelope_sql_static():
    """Verify 0004_redaction_envelope.sql permissions and defaults."""
    sql_path = MIGRATIONS_DIR / "0004_redaction_envelope.sql"
    assert sql_path.is_file(), f"Migration file missing: {sql_path}"

    sql_text = sql_path.read_text(encoding="utf-8")

    # 1. key_id default updated to kms-envelope-v1
    assert "ALTER TABLE redaction_maps ALTER COLUMN key_id SET DEFAULT 'kms-envelope-v1';" in sql_text

    # 2. ticket_rw has INSERT ONLY (no SELECT, UPDATE, DELETE)
    assert "GRANT INSERT ON redaction_maps TO ticket_rw;" in sql_text
    assert "GRANT SELECT ON redaction_maps TO ticket_rw" not in sql_text
    assert "GRANT UPDATE ON redaction_maps TO ticket_rw" not in sql_text
    assert "GRANT DELETE ON redaction_maps TO ticket_rw" not in sql_text

    # 3. queue_rw has DELETE granted for TTL purge
    assert "GRANT DELETE ON redaction_maps TO queue_rw;" in sql_text


# REQ-STORE-009
def test_kms_identity_terraform_module_static():
    """Verify KMS key redaction-map-wrapper configuration in kms-identity module."""
    mod_dir = TF_MODULES_DIR / "kms-identity"
    main_tf = (mod_dir / "main.tf").read_text(encoding="utf-8")
    vars_tf = (mod_dir / "variables.tf").read_text(encoding="utf-8")
    outputs_tf = (mod_dir / "outputs.tf").read_text(encoding="utf-8")

    # Symmetric crypto key definition
    assert 'resource "google_kms_crypto_key" "redaction_map_wrapper"' in main_tf
    assert 'name            = "redaction-map-wrapper"' in main_tf
    assert 'purpose         = "ENCRYPT_DECRYPT"' in main_tf
    assert 'rotation_period = "7776000s"' in main_tf
    assert 'algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"' in main_tf
    assert "prevent_destroy = true" in main_tf

    # IAM member grants
    assert 'resource "google_kms_crypto_key_iam_member" "redaction_encrypters"' in main_tf
    assert 'role          = "roles/cloudkms.cryptoKeyEncrypter"' in main_tf
    assert 'resource "google_kms_crypto_key_iam_member" "redaction_decrypters"' in main_tf
    assert 'role          = "roles/cloudkms.cryptoKeyDecrypter"' in main_tf

    # Variables & Outputs
    assert "redaction_encrypter_members" in vars_tf
    assert "redaction_decrypter_members" in vars_tf
    assert "redaction_map_wrapper_key_id" in outputs_tf


# REQ-STORE-010
def test_redaction_purge_terraform_module_static():
    """Verify Cloud Scheduler job, SA, and IAM grants in redaction-purge module."""
    mod_dir = TF_MODULES_DIR / "redaction-purge"
    assert mod_dir.is_dir(), f"Missing redaction-purge module dir: {mod_dir}"

    versions_tf = (mod_dir / "versions.tf").read_text(encoding="utf-8")
    vars_tf = (mod_dir / "variables.tf").read_text(encoding="utf-8")
    main_tf = (mod_dir / "main.tf").read_text(encoding="utf-8")
    outputs_tf = (mod_dir / "outputs.tf").read_text(encoding="utf-8")

    # Variables
    assert 'variable "human_queue_url"' in vars_tf
    assert 'variable "purge_audience"' in vars_tf

    # Versions constraint
    assert 'required_version = ">= 1.9.0"' in versions_tf
    assert 'version = ">= 6.0.0"' in versions_tf

    # Service Account sa-redaction-purge (length between 6 and 30 chars)
    assert 'resource "google_service_account" "redaction_purge"' in main_tf
    assert 'account_id   = "sa-redaction-purge"' in main_tf
    sa_len = len("sa-redaction-purge")
    assert 6 <= sa_len <= 30

    # Cloud Run Invoker IAM grant for scheduler SA
    assert 'resource "google_cloud_run_v2_service_iam_member" "human_queue_purge_invoker"' in main_tf
    assert 'role     = "roles/run.invoker"' in main_tf

    # Cloud Scheduler job
    assert 'resource "google_cloud_scheduler_job" "redaction_map_purge"' in main_tf
    assert 'schedule         = "15 3 * * *"' in main_tf
    assert 'time_zone        = "UTC"' in main_tf
    assert 'http_method = "POST"' in main_tf
    assert "/internal/purge-expired-redaction-maps" in main_tf
    assert "oidc_token" in main_tf

    # Outputs
    assert "service_account_email" in outputs_tf
    assert "scheduler_job_id" in outputs_tf
    assert "endpoint_url" in outputs_tf


# REQ-STORE-009
def test_mcp_save_redaction_map_tool_contract():
    """Verify ticket-db-mcp save_redaction_map endpoint and schema."""
    repo = FakeTicketRepo()
    app = create_ticket_db_app(ticket_repo=repo)
    client = TestClient(app)

    # 1. Verify schema in /tools
    tools_resp = client.get("/tools")
    assert tools_resp.status_code == 200
    schemas = tools_resp.json()["tools"]
    assert "save_redaction_map" in schemas
    assert "ticket_id" in schemas["save_redaction_map"]["parameters"]["properties"]
    assert "customer_id" in schemas["save_redaction_map"]["parameters"]["properties"]
    assert "envelope_b64" in schemas["save_redaction_map"]["parameters"]["properties"]
    assert "key_id" in schemas["save_redaction_map"]["parameters"]["properties"]

    # 2. Invoke /tools/save_redaction_map
    ticket = repo.create_ticket(TicketCreate(customer_id="cust-mcp-1", body_redacted="Test body"))
    envelope_payload = base64.b64encode(b'{"v":1,"kid":"test-key","wdek":"abc","nonce":"123","ct":"xyz"}').decode("ascii")

    save_resp = client.post(
        "/tools/save_redaction_map",
        json={
            "ticket_id": str(ticket.ticket_id),
            "customer_id": "cust-mcp-1",
            "envelope_b64": envelope_payload,
            "key_id": "kms-envelope-v1",
        },
    )
    assert save_resp.status_code == 200
    data = save_resp.json()
    assert data == {"saved": True}
    # Never echo the envelope back
    assert "envelope_b64" not in data

    # 3. Verify in repository
    stored_map = repo.get_redaction_map(ticket.ticket_id)
    assert stored_map is not None
    assert stored_map.customer_id == "cust-mcp-1"
    assert stored_map.encrypted_mappings == envelope_payload.encode("utf-8")
    assert stored_map.key_id == "kms-envelope-v1"


# REQ-STORE-009
def test_mcp_clients_save_redaction_map():
    """Verify InMemoryTicketDbMcpClient and HttpTicketDbMcpClient save_redaction_map implementations."""
    # InMemory client
    in_mem_client = InMemoryTicketDbMcpClient()
    res = in_mem_client.save_redaction_map(
        ticket_id="ticket-123",
        customer_id="cust-1",
        envelope_b64="dGVzdA==",
    )
    assert res == {"saved": True}

    # HTTP client using TestClient session
    repo = FakeTicketRepo()
    app = create_ticket_db_app(ticket_repo=repo)
    test_client = TestClient(app)

    http_client = HttpTicketDbMcpClient(
        base_url="http://testserver",
        session=test_client,
    )
    http_res = http_client.save_redaction_map(
        ticket_id=str(uuid4()),
        customer_id="cust-http",
        envelope_b64="ZW52ZWxvcGU=",
    )
    assert http_res == {"saved": True}


# REQ-STORE-009
def test_human_queue_reidentify_pii_no_fallback_and_envelope_merging():
    """Verify human_queue reidentify_pii has no fallback and decrypts multiple envelopes."""
    repo = FakeTicketRepo()
    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-reid",
            body_redacted="Message with [EMAIL_ADDRESS_1] and [PHONE_NUMBER_1]",
            assigned_queue="human-urgent",
        )
    )

    cipher = EnvelopeCipher(
        kms_key_name="projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper",
        session=MockKmsSession(),
    )
    sealer = RedactionSealer(cipher=cipher)

    agent = HumanQueueAgent(ticket_repo=repo, envelope_cipher=cipher)

    # Claim ticket
    agent.claim_ticket(ticket.ticket_id, operator_id="operator-elena")

    # Case 1: No map in repo -> returns empty unmasked_fields (NO fake synthesis fallback!)
    no_map_res = agent.reidentify_pii(ticket.ticket_id, operator_id="operator-elena")
    assert no_map_res["unmasked_fields"] == {}

    # Case 2: Save map 1
    env1, kid1 = sealer.seal(str(ticket.ticket_id), {"[EMAIL_ADDRESS_1]": "elena@example.org"})
    from ticket_router.store.models import RedactionMap
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=ticket.ticket_id,
            customer_id="cust-reid",
            encrypted_mappings=env1.encode("utf-8"),
            key_id=kid1,
        )
    )

    # Case 3: Save map 2 (e.g. second user message in conversation)
    env2, kid2 = sealer.seal(str(ticket.ticket_id), {"[PHONE_NUMBER_1]": "+1-555-9876"})
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=ticket.ticket_id,
            customer_id="cust-reid",
            encrypted_mappings=env2.encode("utf-8"),
            key_id=kid2,
        )
    )

    # Reidentify merges both maps
    merged_res = agent.reidentify_pii(ticket.ticket_id, operator_id="operator-elena")
    assert merged_res["unmasked_fields"] == {
        "[EMAIL_ADDRESS_1]": "elena@example.org",
        "[PHONE_NUMBER_1]": "+1-555-9876",
    }
