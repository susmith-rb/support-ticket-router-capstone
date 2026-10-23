"""Static analysis and policy tests for Terraform configurations.

Validates compliance with:
- Conductor Plan 2 (PLAN2.md §Workstream T)
- TDD §4.1 (GCP project structure and regions, us location for Agent Search)
- TDD §12.6 (Static policy checks: no roles/editor, no roles/owner, no google_project_iam_binding)
- TDD §12.7 (Terraform modules structure and interface contracts)
"""

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform"
BOOTSTRAP_DIR = TF_DIR / "bootstrap"
SINGLE_PROJECT_DIR = TF_DIR / "single-project"


def test_terraform_directories_exist():
  """Verify expected Terraform directories exist."""
  assert TF_DIR.is_dir(), f"Missing {TF_DIR}"
  assert BOOTSTRAP_DIR.is_dir(), f"Missing {BOOTSTRAP_DIR}"
  assert SINGLE_PROJECT_DIR.is_dir(), f"Missing {SINGLE_PROJECT_DIR}"
  assert (SINGLE_PROJECT_DIR / "modules" / "project-services").is_dir()
  assert (SINGLE_PROJECT_DIR / "modules" / "agent-search").is_dir()


def test_every_module_has_variables_and_outputs():
  """Every module must provide variables.tf, outputs.tf, and main.tf."""
  modules = [
      BOOTSTRAP_DIR,
      SINGLE_PROJECT_DIR,
      SINGLE_PROJECT_DIR / "modules" / "project-services",
      SINGLE_PROJECT_DIR / "modules" / "agent-search",
  ]
  for mod in modules:
    assert (mod / "main.tf").is_file(), f"Missing main.tf in {mod}"
    assert (mod / "variables.tf").is_file(), f"Missing variables.tf in {mod}"
    assert (mod / "outputs.tf").is_file(), f"Missing outputs.tf in {mod}"


def test_backend_bucket_name():
  """GCS state backend must point to susmith-fde-ticket-router-tfstate."""
  versions_tf = (SINGLE_PROJECT_DIR / "versions.tf").read_text()
  assert 'backend "gcs"' in versions_tf
  bucket_match = re.search(r'bucket\s*=\s*"([^"]+)"', versions_tf)
  assert bucket_match, "backend gcs missing bucket attribute"
  assert bucket_match.group(1) == "susmith-fde-ticket-router-tfstate"

  prefix_match = re.search(r'prefix\s*=\s*"([^"]+)"', versions_tf)
  assert prefix_match, "backend gcs missing prefix attribute"
  assert prefix_match.group(1) == "terraform/state/single-project"


def test_bootstrap_state_bucket_configuration():
  """Bootstrap module must enforce versioning, uniform access, and public prevention."""
  main_tf = (BOOTSTRAP_DIR / "main.tf").read_text()
  vars_tf = (BOOTSTRAP_DIR / "variables.tf").read_text()

  assert "google_storage_bucket" in main_tf
  assert "versioning" in main_tf
  assert "uniform_bucket_level_access = true" in main_tf
  assert 'public_access_prevention    = "enforced"' in main_tf or 'public_access_prevention = "enforced"' in main_tf

  # Check default bucket name
  assert 'default     = "susmith-fde-ticket-router-tfstate"' in vars_tf


def test_no_global_location_when_search_location_is_us():
  """Agent Search resources must be in location 'us', never hardcoding 'global'."""
  as_main = (SINGLE_PROJECT_DIR / "modules" / "agent-search" / "main.tf").read_text()
  as_vars = (SINGLE_PROJECT_DIR / "modules" / "agent-search" / "variables.tf").read_text()
  sp_vars = (SINGLE_PROJECT_DIR / "variables.tf").read_text()

  # Defaults must be "us"
  assert 'default     = "us"' in as_vars
  assert 'default     = "us"' in sp_vars

  # Neither data store nor engine should hardcode "global"
  assert 'location                    = "global"' not in as_main
  assert 'location         = "global"' not in as_main
  assert 'location = "global"' not in as_main

  # Resources must reference var.search_location
  assert "location                    = var.search_location" in as_main
  assert "location          = var.search_location" in as_main


def test_tdd_12_6_security_policy_no_forbidden_iam():
  """TDD §12.6 policy: no google_project_iam_binding, no roles/editor, no roles/owner."""
  tf_files = list(TF_DIR.glob("**/*.tf"))
  assert len(tf_files) > 0, "No .tf files found"

  forbidden_patterns = [
      ("google_project_iam_binding", "Authoritative project IAM binding is forbidden; use member"),
      ("roles/editor", "roles/editor is forbidden per security policy"),
      ("roles/owner", "roles/owner is forbidden per security policy"),
  ]

  violations = []
  for tf_file in tf_files:
    content = tf_file.read_text()
    for pattern, reason in forbidden_patterns:
      if pattern in content:
        violations.append(f"{tf_file.relative_to(REPO_ROOT)}: {pattern} ({reason})")

  assert not violations, "Security policy violations found:\n" + "\n".join(violations)


def test_agent_search_contract():
  """Agent Search module must declare faq-articles, resolved-threads, and support-kb-search."""
  main_tf = (SINGLE_PROJECT_DIR / "modules" / "agent-search" / "main.tf").read_text()

  # Data store: faq-articles (requires unstructured content for text matching)
  assert 'data_store_id               = "faq-articles"' in main_tf
  assert 'industry_vertical           = "GENERIC"' in main_tf
  assert 'content_config              = "CONTENT_REQUIRED"' in main_tf
  assert 'solution_types              = ["SOLUTION_TYPE_SEARCH"]' in main_tf

  # Data store: resolved-threads (structured data only, NO_CONTENT)
  assert 'data_store_id               = "resolved-threads"' in main_tf
  assert 'content_config              = "NO_CONTENT"' in main_tf

  # Search engine: support-kb-search
  assert 'engine_id         = "support-kb-search"' in main_tf
  assert 'collection_id     = "default_collection"' in main_tf
  assert "data_store_ids = [" in main_tf
  assert "faq_articles.data_store_id" in main_tf
  assert "resolved_threads.data_store_id" in main_tf
  assert "search_engine_config {" in main_tf
  assert "search_tier    = var.search_tier" in main_tf


def test_resolved_threads_schema_matches_content_config():
  """Assert resolved-threads content_config matches schema fixture (NO_CONTENT iff no 'content' key)."""
  import json
  # Tiny fixture copied from the real resolved-threads schema / import JSONL:
  # structData only (thread_id, text, etc.), no "content" key.
  sample_line = (
      '{"id": "thread-272", "structData": {"thread_id": "272", "text": "Customer: problem\\nAgent: reply"}}'
  )
  record = json.loads(sample_line)

  main_tf = (SINGLE_PROJECT_DIR / "modules" / "agent-search" / "main.tf").read_text()
  match = re.search(
      r'resource\s+"google_discovery_engine_data_store"\s+"resolved_threads"\s+\{[^}]+content_config\s+=\s+"([^"]+)"',
      main_tf,
  )
  assert match, "Could not find content_config in google_discovery_engine_data_store.resolved_threads"
  configured_content_config = match.group(1)

  has_content_key = "content" in record
  if not has_content_key:
    assert configured_content_config == "NO_CONTENT", (
        f"Record has no 'content' key, expected content_config='NO_CONTENT', got {configured_content_config}"
    )
  else:
    assert configured_content_config == "CONTENT_REQUIRED", (
        f"Record has 'content' key, expected content_config='CONTENT_REQUIRED', got {configured_content_config}"
    )


def test_corpus_bucket_data_source_not_managed():
  """Corpus bucket must only be referenced as data source, not managed resource."""
  single_main = (SINGLE_PROJECT_DIR / "main.tf").read_text()
  assert 'data "google_storage_bucket" "corpus_bucket"' in single_main
  assert 'resource "google_storage_bucket" "corpus' not in single_main
