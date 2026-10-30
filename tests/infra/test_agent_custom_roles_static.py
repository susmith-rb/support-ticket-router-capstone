"""Static verification of least-privilege agent custom roles and SA key restrictions.

# REQ-AUTH-001: Least-Privilege Custom Inference Roles (replace roles/aiplatform.user)
# REQ-AUTH-005: Stated Service-Account Exceptions (no service account keys)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_IAM_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project" / "modules" / "agents-iam"
TF_ROOT = REPO_ROOT / "deploy" / "terraform"

EXPECTED_INFERENCE_PERMISSIONS = {
    "aiplatform.endpoints.predict",
}

EXPECTED_TRIAGE_PERMISSIONS = {
    "aiplatform.endpoints.predict",
    "aiplatform.sessions.create",
    "aiplatform.sessions.get",
    "aiplatform.sessions.list",
    "aiplatform.sessions.update",
    "aiplatform.sessions.delete",
    "aiplatform.sessions.run",
    "aiplatform.sessionEvents.append",
    "aiplatform.sessionEvents.list",
}


def _extract_local_list(content: str, local_name: str) -> list[str]:
    """Extract string elements from a Terraform local list definition."""
    pattern = rf"{local_name}\s*=\s*\[([^\]]*)\]"
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


# REQ-AUTH-001
def test_agents_use_enumerated_custom_roles():
    """Verify agent identities use enumerated custom roles instead of roles/aiplatform.user.

    Invariants:
    1. Neither sa-triage nor sa-faq holds roles/aiplatform.user in agents-iam.
    2. Two google_project_iam_custom_role resources are defined:
       - role_id ticketRouterAgentInference with title "Ticket Router Agent Inference"
       - role_id ticketRouterTriageRuntime with title "Ticket Router Triage Runtime"
    3. Exactly the enumerated permissions are granted (order-insensitive).
    4. sa_triage and sa_faq are bound to the custom roles via google_project_iam_member.
    5. Endpoint-scoped roles/aiplatform.predictionUser bindings on router-s1 remain intact.
    """
    main_tf = (AGENTS_IAM_DIR / "main.tf").read_text(encoding="utf-8")
    outputs_tf = (AGENTS_IAM_DIR / "outputs.tf").read_text(encoding="utf-8")

    # Invariant 1: No roles/aiplatform.user in agents-iam module
    assert "roles/aiplatform.user" not in main_tf, (
        "roles/aiplatform.user must not be granted to any agent identity in agents-iam/main.tf"
    )

    # Invariant 2 & 3: Permissions in locals and custom role definitions
    inference_perms = _extract_local_list(main_tf, "agent_inference_permissions")
    assert inference_perms, "agent_inference_permissions local list missing in agents-iam/main.tf"
    assert set(inference_perms) == EXPECTED_INFERENCE_PERMISSIONS, (
        f"agent_inference_permissions mismatch: got {inference_perms}, expected {EXPECTED_INFERENCE_PERMISSIONS}"
    )
    assert len(inference_perms) == len(EXPECTED_INFERENCE_PERMISSIONS), (
        "Duplicate or extraneous permissions in agent_inference_permissions"
    )

    triage_perms = _extract_local_list(main_tf, "triage_runtime_permissions")
    assert triage_perms, "triage_runtime_permissions local list missing in agents-iam/main.tf"
    assert set(triage_perms) == EXPECTED_TRIAGE_PERMISSIONS, (
        f"triage_runtime_permissions mismatch: got {triage_perms}, expected {EXPECTED_TRIAGE_PERMISSIONS}"
    )
    assert len(triage_perms) == len(EXPECTED_TRIAGE_PERMISSIONS), (
        "Duplicate or extraneous permissions in triage_runtime_permissions"
    )

    # Verify custom role resources
    assert 'resource "google_project_iam_custom_role" "agent_inference"' in main_tf
    assert 'role_id     = "ticketRouterAgentInference"' in main_tf
    assert 'title       = "Ticket Router Agent Inference"' in main_tf
    assert "permissions = local.agent_inference_permissions" in main_tf

    assert 'resource "google_project_iam_custom_role" "triage_runtime"' in main_tf
    assert 'role_id     = "ticketRouterTriageRuntime"' in main_tf
    assert 'title       = "Ticket Router Triage Runtime"' in main_tf
    assert "permissions = local.triage_runtime_permissions" in main_tf

    # Invariant 4: sa_triage and sa_faq are bound to custom roles
    assert 'resource "google_project_iam_member" "triage_runtime_custom_role"' in main_tf
    assert (
        "role    = google_project_iam_custom_role.triage_runtime.id" in main_tf
        or "role    = google_project_iam_custom_role.triage_runtime.name" in main_tf
    )
    assert "google_service_account.sa_triage.email" in main_tf

    assert 'resource "google_project_iam_member" "faq_inference_custom_role"' in main_tf
    assert (
        "role    = google_project_iam_custom_role.agent_inference.id" in main_tf
        or "role    = google_project_iam_custom_role.agent_inference.name" in main_tf
    )
    assert "google_service_account.sa_faq.email" in main_tf

    # Invariant 5: Endpoint-scoped predictionUser bindings remain intact
    assert 'resource "google_vertex_ai_endpoint_iam_member" "triage_prediction_user"' in main_tf
    assert 'resource "google_vertex_ai_endpoint_iam_member" "faq_prediction_user"' in main_tf
    assert 'resource "google_vertex_ai_endpoint_iam_member" "triage_dedicated_prediction_user"' in main_tf
    assert 'resource "google_vertex_ai_endpoint_iam_member" "faq_dedicated_prediction_user"' in main_tf
    assert 'role     = "roles/aiplatform.predictionUser"' in main_tf

    # Outputs exported
    assert "agent_inference_custom_role_id" in outputs_tf
    assert "triage_runtime_custom_role_id" in outputs_tf


# REQ-AUTH-005
def test_no_service_account_keys():
    """Verify no google_service_account_key resource exists anywhere under deploy/terraform.

    Enforces REQ-AUTH-005: all service accounts must be keyless, complying with org policy
    iam.disableServiceAccountKeyCreation.
    """
    assert TF_ROOT.is_dir(), f"Terraform root not found: {TF_ROOT}"
    tf_files = list(TF_ROOT.rglob("*.tf"))
    assert tf_files, f"No .tf files found in {TF_ROOT}"

    violations = []
    for tf_file in tf_files:
        text = tf_file.read_text(encoding="utf-8")
        if "google_service_account_key" in text:
            violations.append(str(tf_file.relative_to(REPO_ROOT)))

    assert not violations, (
        f"Found google_service_account_key in {violations}. "
        "REQ-AUTH-005 strictly forbids static service account keys in Terraform."
    )
