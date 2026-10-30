"""Static verification of SPIFFE Agent Identity Terraform grants for TriageRouter.

# REQ-AUTH-004: SPIFFE Agent Identity for TriageRouter (Agent Runtime)
Verifies:
1. Root variable `triage_agent_principal` defaults to `""` (zero-diff plan by default).
2. Root `main.tf` wires `var.triage_agent_principal` into owning modules (`agents_iam`,
   `kms_identity`, `cloud_run_services`, `model_armor`).
3. Every IAM grant held by `sa-triage` has a conditional counterpart for
   `triage_agent_principal` passing the exact `principal://...` member string,
   plus `roles/iam.serviceAccountOpenIdTokenCreator` on `sa-triage`.
4. Least-privilege boundaries hold (no order-lookup-mcp invoker, no discoveryengine,
   no aiplatform.user, no broad serviceAccountTokenCreator).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"
MOD_DIR = TF_DIR / "modules"


def _extract_block(text: str, header_regex: str) -> str:
    """Extract a balanced-brace HCL block matching header_regex."""
    match = re.search(header_regex + r"\s*\{", text)
    assert match is not None, f"Missing HCL block matching: {header_regex}"
    start = match.end()
    depth = 1
    idx = start
    while idx < len(text) and depth > 0:
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"Unbalanced braces in block: {header_regex}"
    return text[start : idx - 1]


# REQ-AUTH-004
def test_root_variable_and_module_wiring_for_triage_agent_principal() -> None:
    """Root triage_agent_principal variable defaults to empty string and wires into owning modules."""
    root_vars = (TF_DIR / "variables.tf").read_text(encoding="utf-8")
    var_block = _extract_block(root_vars, r'variable\s+"triage_agent_principal"')
    assert re.search(r"type\s*=\s*string\b", var_block)
    assert re.search(r'default\s*=\s*""', var_block)

    root_main = (TF_DIR / "main.tf").read_text(encoding="utf-8")

    for mod_name in ("agents_iam", "cloud_run_services", "model_armor"):
        mod_block = _extract_block(root_main, rf'module\s+"{mod_name}"')
        assert re.search(
            r"triage_agent_principal\s*=\s*var\.triage_agent_principal\b",
            mod_block,
        ), f"module {mod_name} does not wire var.triage_agent_principal"

    kms_block = _extract_block(root_main, r'module\s+"kms_identity"')
    assert re.search(
        r'signer_members\s*=\s*compact\(\[\s*"serviceAccount:\$\{module\.agents_iam\.sa_triage_email\}"\s*,\s*var\.triage_agent_principal\s*\]\)',
        kms_block,
    ), "kms_identity.signer_members must use compact([...sa_triage_email, var.triage_agent_principal]) for zero-diff default"

    for submod in ("agents-iam", "cloud-run-services", "model-armor"):
        sub_vars = (MOD_DIR / submod / "variables.tf").read_text(encoding="utf-8")
        sub_var_block = _extract_block(sub_vars, r'variable\s+"triage_agent_principal"')
        assert re.search(r"type\s*=\s*string\b", sub_var_block)
        assert re.search(r'default\s*=\s*""', sub_var_block)


# REQ-AUTH-004
@pytest.mark.parametrize(
    ("module_rel", "res_type", "res_name", "expected_role_pattern", "extra_patterns"),
    [
        # 1. Project custom role ticketRouterTriageRuntime
        (
            "agents-iam/main.tf",
            "google_project_iam_member",
            "triage_agent_runtime_custom_role",
            r"role\s*=\s*google_project_iam_custom_role\.triage_runtime\.id\b",
            [],
        ),
        # 2. Endpoint-scoped predictionUser on router-s1
        (
            "agents-iam/main.tf",
            "google_vertex_ai_endpoint_iam_member",
            "triage_agent_prediction_user",
            r'role\s*=\s*"roles/aiplatform\.predictionUser"',
            [r"provider\s*=\s*google-beta\b", r"endpoint\s*=\s*var\.router_endpoint_id\b"],
        ),
        # 3. Endpoint-scoped predictionUser on router-s1-dedicated
        (
            "agents-iam/main.tf",
            "google_vertex_ai_endpoint_iam_member",
            "triage_agent_dedicated_prediction_user",
            r'role\s*=\s*"roles/aiplatform\.predictionUser"',
            [r"provider\s*=\s*google-beta\b", r"endpoint\s*=\s*var\.dedicated_router_endpoint_id\b"],
        ),
        # 4. Pub/Sub publisher on human-queue topic
        (
            "agents-iam/main.tf",
            "google_pubsub_topic_iam_member",
            "triage_agent_human_queue_publisher",
            r'role\s*=\s*"roles/pubsub\.publisher"',
            [r"topic\s*=\s*var\.pubsub_human_queue_topic_name\b"],
        ),
        # 5. Cloud Trace agent role
        (
            "agents-iam/main.tf",
            "google_project_iam_member",
            "triage_agent_cloudtrace_agent",
            r'role\s*=\s*"roles/cloudtrace\.agent"',
            [],
        ),
        # 6. Service Account OpenID Token Creator on sa-triage (for generateIdToken)
        (
            "agents-iam/main.tf",
            "google_service_account_iam_member",
            "triage_agent_sa_openid_token_creator",
            r'role\s*=\s*"roles/iam\.serviceAccountOpenIdTokenCreator"',
            [r"service_account_id\s*=\s*google_service_account\.sa_triage\.name\b"],
        ),
        # 7. Model Armor user role
        (
            "model-armor/main.tf",
            "google_project_iam_member",
            "model_armor_user_triage_agent",
            r'role\s*=\s*"roles/modelarmor\.user"',
            [],
        ),
        # 8. Cloud Run invoker on ticket-db-mcp
        (
            "cloud-run-services/main.tf",
            "google_cloud_run_v2_service_iam_member",
            "ticket_db_invoker_triage_agent",
            r'role\s*=\s*"roles/run\.invoker"',
            [r"name\s*=\s*google_cloud_run_v2_service\.ticket_db_mcp\[0\]\.name\b"],
        ),
        # 9. Cloud Run invoker on human-queue
        (
            "cloud-run-services/main.tf",
            "google_cloud_run_v2_service_iam_member",
            "human_queue_invoker_triage_agent",
            r'role\s*=\s*"roles/run\.invoker"',
            [r"name\s*=\s*google_cloud_run_v2_service\.human_queue\[0\]\.name\b"],
        ),
    ],
)
def test_conditional_triage_agent_iam_grants(
    module_rel: str,
    res_type: str,
    res_name: str,
    expected_role_pattern: str,
    extra_patterns: list[str],
) -> None:
    """Each explicit triage_agent_principal IAM resource is conditional on != \"\" and passes exact principal."""
    tf_text = (MOD_DIR / module_rel).read_text(encoding="utf-8")
    block = _extract_block(tf_text, rf'resource\s+"{res_type}"\s+"{res_name}"')

    # Must be gated behind var.triage_agent_principal != ""
    assert re.search(
        r'count\s*=.*var\.triage_agent_principal\s*!=\s*"".*\?\s*1\s*:\s*0',
        block,
    ), f"{res_type}.{res_name} is missing conditional count gating on var.triage_agent_principal != \"\""

    # Must use var.triage_agent_principal verbatim (no serviceAccount: prefix!)
    assert re.search(
        r"member\s*=\s*var\.triage_agent_principal\b",
        block,
    ), f"{res_type}.{res_name} must pass member = var.triage_agent_principal directly"
    assert "serviceAccount:" not in block

    # Role and resource targets must match sa-triage's grant
    assert re.search(expected_role_pattern, block), (
        f"{res_type}.{res_name} did not match role pattern {expected_role_pattern}"
    )
    for pat in extra_patterns:
        assert re.search(pat, block), f"{res_type}.{res_name} missing attribute matching {pat}"


# REQ-AUTH-004
def test_kms_signer_grant_parity_for_triage_agent_principal() -> None:
    """Verify 10th grant: KMS customer-context-signer roles/cloudkms.signer receives exact member string."""
    kms_main = (MOD_DIR / "kms-identity" / "main.tf").read_text(encoding="utf-8")
    signers_block = _extract_block(
        kms_main,
        r'resource\s+"google_kms_crypto_key_iam_member"\s+"signers"',
    )
    assert re.search(r"for_each\s*=\s*toset\(var\.signer_members\)", signers_block)
    assert re.search(r'role\s*=\s*"roles/cloudkms\.signer"', signers_block)
    assert re.search(r"member\s*=\s*each\.value\b", signers_block)


# REQ-AUTH-004
def test_triage_agent_principal_least_privilege_boundaries() -> None:
    """triage_agent_principal must not receive FAQ-only or overly broad roles."""
    cr_main = (MOD_DIR / "cloud-run-services" / "main.tf").read_text(encoding="utf-8")
    iam_main = (MOD_DIR / "agents-iam" / "main.tf").read_text(encoding="utf-8")

    # Never add triage_agent_principal to ticket_db_invokers (which prepends serviceAccount:)
    invokers_match = re.search(
        r"ticket_db_invokers\s*=\s*distinct\(compact\(\[([^\]]*)\]\)\)",
        cr_main,
        re.DOTALL,
    )
    assert invokers_match is not None
    assert "triage_agent_principal" not in invokers_match.group(1)
    assert "var.triage_agent_principal" not in cr_main.split("# 2. Order Lookup MCP Server")[1].split(
        "# 3. Human Queue Agent Service"
    )[0], "triage_agent_principal must NOT be granted roles/run.invoker on order-lookup-mcp"

    # Least-privilege SA impersonation: OpenIdTokenCreator only, never broad TokenCreator in agents-iam
    assert "roles/iam.serviceAccountTokenCreator" not in iam_main
    assert "roles/aiplatform.user" not in iam_main

    # Discovery Engine viewer belongs solely to sa-faq
    faq_de_block = _extract_block(
        iam_main,
        r'resource\s+"google_project_iam_member"\s+"faq_discovery_engine_viewer"',
    )
    assert "triage_agent_principal" not in faq_de_block
    assert iam_main.count("roles/discoveryengine.viewer") == 1


# REQ-AUTH-004
def test_exhaustive_sa_triage_grant_parity_across_all_tf_files() -> None:
    """Scan all single-project .tf files to guarantee no sa-triage IAM member grant was missed."""
    expected_sa_triage_member_resources = {
        ("modules/agents-iam/main.tf", "triage_prediction_user"),
        ("modules/agents-iam/main.tf", "triage_dedicated_prediction_user"),
        ("modules/agents-iam/main.tf", "triage_runtime_custom_role"),
        ("modules/agents-iam/main.tf", "triage_human_queue_publisher"),
        ("modules/agents-iam/main.tf", "triage_cloudtrace_agent"),
        ("modules/model-armor/main.tf", "model_armor_user_triage"),
        ("modules/cloud-run-services/main.tf", "human_queue_invoker"),
    }
    discovered: set[tuple[str, str]] = set()
    res_header_re = re.compile(r'resource\s+"([^"]+_iam_member)"\s+"([^"]+)"\s*\{')

    for tf_path in sorted(TF_DIR.rglob("*.tf")):
        rel = tf_path.relative_to(TF_DIR).as_posix()
        text = tf_path.read_text(encoding="utf-8")
        for m in res_header_re.finditer(text):
            res_type, res_name = m.group(1), m.group(2)
            block = _extract_block(text, rf'resource\s+"{re.escape(res_type)}"\s+"{re.escape(res_name)}"')
            member_lines = [ln for ln in block.splitlines() if re.match(r"^\s*member\s*=", ln)]
            assert len(member_lines) == 1
            member_expr = member_lines[0]
            if any(tok in member_expr for tok in ("sa_triage", "triage_sa_email")):
                discovered.add((rel, res_name))

    # Indirect loop grants (ticket_db_invoker via ticket_db_invokers, and kms signers via signer_members)
    # are tested separately above; direct IAM member blocks must match the exact 7 expected resources.
    assert discovered == expected_sa_triage_member_resources, (
        f"Unexpected or unmirrored sa-triage IAM member grants: {discovered ^ expected_sa_triage_member_resources}"
    )
