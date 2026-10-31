
"""Static analysis and verification for Customer Context Token (CCT) KMS Infrastructure.

# REQ-AUTH-002: Signed Customer Context Token (minting at TriageRouter)
# REQ-AUTH-003: CCT Transport and Verification at order-lookup-mcp (RLS from verified claims)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
KMS_MOD_DIR = TF_DIR / "modules" / "kms-identity"
CR_MOD_DIR = TF_DIR / "modules" / "cloud-run-services"
SERVICES_MOD_DIR = TF_DIR / "modules" / "project-services"
ROOT_MAIN = TF_DIR / "main.tf"
ROOT_OUTPUTS = TF_DIR / "outputs.tf"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_agents" / "deploy_triage_agent_runtime.py"


# REQ-AUTH-002
def test_kms_identity_module_structure():
    """Verify KMS identity Terraform module layout and provider constraints."""
    assert KMS_MOD_DIR.is_dir(), f"Missing module directory: {KMS_MOD_DIR}"

    main_tf = KMS_MOD_DIR / "main.tf"
    vars_tf = KMS_MOD_DIR / "variables.tf"
    outputs_tf = KMS_MOD_DIR / "outputs.tf"
    versions_tf = KMS_MOD_DIR / "versions.tf"

    for file_path in [main_tf, vars_tf, outputs_tf, versions_tf]:
        assert file_path.is_file(), f"Missing expected module file: {file_path}"

    versions_content = versions_tf.read_text(encoding="utf-8")
    assert 'required_version = ">= 1.9.0"' in versions_content
    assert '"hashicorp/google"' in versions_content
    assert 'version = ">= 6.50.0"' in versions_content


# REQ-AUTH-002
def test_kms_identity_crypto_key_and_keyring():
    """Verify key ring, asymmetric signing crypto key, algorithm, and lifecycle prevent_destroy."""
    main_content = (KMS_MOD_DIR / "main.tf").read_text(encoding="utf-8")

    # 1. Key Ring
    assert 'resource "google_kms_key_ring" "ticket_router_identity"' in main_content
    assert 'name     = "ticket-router-identity"' in main_content
    assert "location = var.region" in main_content

    # 2. Crypto Key
    assert 'resource "google_kms_crypto_key" "customer_context_signer"' in main_content
    assert 'name     = "customer-context-signer"' in main_content
    assert 'purpose  = "ASYMMETRIC_SIGN"' in main_content

    # 3. Version template (EC_SIGN_P256_SHA256, SOFTWARE)
    assert 'algorithm        = "EC_SIGN_P256_SHA256"' in main_content or 'algorithm = "EC_SIGN_P256_SHA256"' in main_content
    assert 'protection_level = "SOFTWARE"' in main_content or 'protection_level = "SOFTWARE"' in main_content

    # 4. Lifecycle prevent_destroy = true
    assert "prevent_destroy = true" in main_content

    # 5. Comment on key ring / key permanence in Cloud KMS (Open Question 1)
    assert "Open Question 1" in main_content or "cannot be deleted" in main_content


# REQ-AUTH-002
# REQ-AUTH-003
def test_kms_identity_iam_least_privilege():
    """Verify least-privilege IAM roles for signer (sa-triage) and verifier (sa-order-mcp)."""
    main_content = (KMS_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_content = (KMS_MOD_DIR / "variables.tf").read_text(encoding="utf-8")

    assert "signer_members" in vars_content
    assert "verifier_members" in vars_content

    # Signer role
    assert 'resource "google_kms_crypto_key_iam_member" "signers"' in main_content
    assert 'role          = "roles/cloudkms.signer"' in main_content
    assert "toset(var.signer_members)" in main_content

    # Verifier / public key viewer role
    assert 'resource "google_kms_crypto_key_iam_member" "verifiers"' in main_content
    assert 'role          = "roles/cloudkms.publicKeyViewer"' in main_content
    assert "toset(var.verifier_members)" in main_content

    # Invariant: No overly broad admin or decryptor roles
    for forbidden in [
        "roles/cloudkms.admin",
        "roles/cloudkms.cryptoKeyEncrypterDecrypter",
        "roles/owner",
        "roles/editor",
    ]:
        assert forbidden not in main_content, f"Forbidden role found in kms-identity: {forbidden}"


# REQ-AUTH-002
# REQ-AUTH-003
def test_kms_identity_module_outputs():
    """Verify kms-identity outputs provide signing key version and key prefix."""
    outputs_content = (KMS_MOD_DIR / "outputs.tf").read_text(encoding="utf-8")

    assert 'output "key_ring_id"' in outputs_content
    assert 'output "crypto_key_id"' in outputs_content
    assert 'output "signing_key_version"' in outputs_content
    assert 'output "key_prefix"' in outputs_content

    assert "/cryptoKeyVersions/1" in outputs_content
    assert "key_prefix" in outputs_content


# REQ-AUTH-002
# REQ-AUTH-003
def test_root_wiring_and_cloudkms_service():
    """Verify cloudkms API enabled and kms_identity wired into root main.tf and outputs.tf."""
    root_main = ROOT_MAIN.read_text(encoding="utf-8")
    root_outputs = ROOT_OUTPUTS.read_text(encoding="utf-8")
    services_vars = (SERVICES_MOD_DIR / "variables.tf").read_text(encoding="utf-8")

    # cloudkms enabled in services
    assert '"cloudkms.googleapis.com"' in root_main
    assert '"cloudkms.googleapis.com"' in services_vars

    # kms_identity module declared and wired
    assert 'module "kms_identity"' in root_main
    assert re.search(r'source\s*=\s*"\./modules/kms-identity"', root_main)
    assert "module.agents_iam.sa_triage_email" in root_main
    assert "module.cloud_sql.sa_order_mcp_email" in root_main

    # root outputs
    assert 'output "signing_key_version"' in root_outputs
    assert 'output "key_prefix"' in root_outputs
    assert "module.kms_identity.signing_key_version" in root_outputs
    assert "module.kms_identity.key_prefix" in root_outputs


# REQ-AUTH-003
def test_cloud_run_order_lookup_cct_env_vars():
    """Verify order-lookup Cloud Run service configures CCT verification environment variables."""
    cr_main = (CR_MOD_DIR / "main.tf").read_text(encoding="utf-8")
    cr_vars = (CR_MOD_DIR / "variables.tf").read_text(encoding="utf-8")
    root_main = ROOT_MAIN.read_text(encoding="utf-8")

    # Variable definitions
    assert "variable \"cct_mode\"" in cr_vars
    assert 'default     = "accept"' in cr_vars
    assert "variable \"cct_key_prefix\"" in cr_vars

    # Environment variables in order-lookup-mcp container
    assert 'name  = "CCT_MODE"' in cr_main
    assert "value = var.cct_mode" in cr_main
    assert 'name  = "CCT_AUDIENCE"' in cr_main
    assert 'value = "order-lookup-mcp"' in cr_main
    assert 'name  = "CCT_ALLOWED_ISSUERS"' in cr_main
    assert 'value = "triage-router"' in cr_main
    assert 'name  = "CCT_KEY_PREFIX"' in cr_main
    assert "value = var.cct_key_prefix" in cr_main

    # Root module passes cct_key_prefix from kms_identity
    assert re.search(r"cct_key_prefix\s+=\s+module\.kms_identity\.key_prefix", root_main)

    # Invariant: Lifecycle ignore_changes is preserved on order_lookup_mcp
    assert "lifecycle" in cr_main
    assert "template[0].containers[0].image" in cr_main


# REQ-AUTH-002
def test_deploy_triage_agent_runtime_cct_contract():
    """Verify deploy_triage_agent_runtime.py declares CCT contract variables and cryptography dependency."""
    assert DEPLOY_SCRIPT.is_file(), f"Missing deploy script: {DEPLOY_SCRIPT}"
    script_content = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # Pinned requirement
    assert "cryptography>=42.0.0" in script_content

    # Contract environment variables
    for cct_var in ["CCT_KEY_VERSION", "CCT_ISSUER", "CCT_AUDIENCE", "CCT_TTL_S"]:
        assert cct_var in script_content, f"Missing {cct_var} in deploy_triage_agent_runtime.py"

    # Default values
    assert "ticket-router-identity" in script_content
    assert "customer-context-signer" in script_content
    assert "cryptoKeyVersions/1" in script_content
    assert 'DEFAULT_CCT_ISSUER = "triage-router"' in script_content
    assert 'DEFAULT_CCT_AUDIENCE = "order-lookup-mcp"' in script_content
    assert 'DEFAULT_CCT_TTL_S = "300"' in script_content

    # CLI argument parsing
    assert "--cct-key-version" in script_content
    assert "--cct-issuer" in script_content
    assert "--cct-audience" in script_content
    assert "--cct-ttl-s" in script_content
