"""Static infrastructure tests for Cloud Identity Platform Terraform module.

# REQ-IDENT-001: Cloud Identity Platform IdP Configuration & Token Format
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_MODULE_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project" / "modules" / "identity"
ROOT_TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"


# REQ-IDENT-001
def test_identity_module_files_exist():
    """Verify identity module has all required Terraform files."""
    assert (IDENTITY_MODULE_DIR / "versions.tf").is_file(), "versions.tf missing in identity module"
    assert (IDENTITY_MODULE_DIR / "variables.tf").is_file(), "variables.tf missing in identity module"
    assert (IDENTITY_MODULE_DIR / "main.tf").is_file(), "main.tf missing in identity module"
    assert (IDENTITY_MODULE_DIR / "outputs.tf").is_file(), "outputs.tf missing in identity module"


# REQ-IDENT-001
def test_identity_module_versions():
    """Verify versions.tf has required_version and providers."""
    versions_text = (IDENTITY_MODULE_DIR / "versions.tf").read_text()
    assert 'required_version = ">= 1.9.0"' in versions_text
    assert "google = {" in versions_text
    assert "google-beta = {" in versions_text


# REQ-IDENT-001
def test_identity_module_resources():
    """Verify Identity Platform, API key, and Secret Manager configurations."""
    main_text = (IDENTITY_MODULE_DIR / "main.tf").read_text()

    # Identity Platform config
    assert 'resource "google_identity_platform_config" "default"' in main_text
    assert "provider = google-beta" in main_text
    assert "sign_in {" in main_text
    assert "enabled           = true" in main_text or "enabled = true" in main_text
    assert "password_required = true" in main_text
    assert "multi_tenant {" in main_text
    assert "allow_tenants = false" in main_text
    assert "authorized_domains" in main_text
    assert "localhost" in main_text
    assert "faq.8-232-184-225.sslip.io" in main_text

    # Restricted API key
    assert 'resource "google_apikeys_key" "identity_web"' in main_text
    assert 'service = "identitytoolkit.googleapis.com"' in main_text
    assert 'service = "securetoken.googleapis.com"' in main_text

    # Secret Manager secret for API key
    assert 'resource "google_secret_manager_secret" "identity_web_api_key"' in main_text
    assert 'secret_id = "identity-web-api-key"' in main_text
    assert 'resource "google_secret_manager_secret_version" "identity_web_api_key"' in main_text

    # Output definition
    outputs_text = (IDENTITY_MODULE_DIR / "outputs.tf").read_text()
    assert 'output "identity_web_api_key_secret_id"' in outputs_text


# REQ-IDENT-001
def test_root_terraform_wiring():
    """Verify root main.tf and outputs.tf correctly wire the identity module."""
    root_main = (ROOT_TF_DIR / "main.tf").read_text()
    assert '"identitytoolkit.googleapis.com"' in root_main
    assert '"apikeys.googleapis.com"' in root_main
    assert 'module "identity" {' in root_main
    assert 'source     = "./modules/identity"' in root_main or 'source = "./modules/identity"' in root_main

    root_outputs = (ROOT_TF_DIR / "outputs.tf").read_text()
    assert 'output "identity_web_api_key_secret_id"' in root_outputs
