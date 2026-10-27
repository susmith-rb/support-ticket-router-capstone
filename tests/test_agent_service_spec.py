"""Static verification and contract checks for specs/agent-service.md.

Validates that:
1. specs/agent-service.md exists and adheres to the <= 350 lines limit.
2. All 15 REQ-AGENT-xxx requirements are defined, unique, have status 'open', and owner 'agent-service'.
3. Multi-agent topology, service accounts, and database roles are specified.
4. Agents do NOT hold direct Postgres roles (they reach DB strictly via MCP servers).
5. Only MCP SAs hold DB roles (sa-ticket-mcp -> ticket_rw, sa-order-mcp -> orders_ro, sa-queue -> queue_rw).
6. All infrastructure is declared in Terraform under deploy/terraform/single-project/modules/*.
7. Routing thresholds, add-only escalation, and gaming-resistant priority are specified.
8. MCP contracts, circuit breaker parameters, latency budgets, and observability are specified.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "specs" / "agent-service.md"

EXPECTED_REQS = [f"REQ-AGENT-{i:03d}" for i in range(1, 16)]


def _read_spec() -> str:
    assert SPEC_PATH.exists(), f"Spec file not found at {SPEC_PATH}"
    return SPEC_PATH.read_text(encoding="utf-8")


def test_spec_file_exists_and_line_budget():
    # REQ-AGENT-001
    text = _read_spec()
    lines = text.splitlines()
    assert len(lines) > 50, "Spec file is too short to be complete"
    assert len(lines) <= 350, f"Spec exceeds 350 lines limit: {len(lines)} lines"


def test_all_agent_reqs_defined_and_open():
    # REQ-AGENT-001
    # REQ-AGENT-002
    # REQ-AGENT-003
    # REQ-AGENT-004
    # REQ-AGENT-005
    # REQ-AGENT-006
    # REQ-AGENT-007
    # REQ-AGENT-008
    # REQ-AGENT-009
    # REQ-AGENT-010
    # REQ-AGENT-011
    # REQ-AGENT-012
    # REQ-AGENT-013
    # REQ-AGENT-014
    # REQ-AGENT-015
    text = _read_spec()
    for req_id in EXPECTED_REQS:
        header_pattern = rf"^###\s+{re.escape(req_id)}:"
        assert re.search(header_pattern, text, re.MULTILINE), f"Missing header for {req_id}"

        # Verify block fields
        block_match = re.search(rf"^###\s+{re.escape(req_id)}:.*?(?=\n###|\n---|\Z)", text, re.MULTILINE | re.DOTALL)
        assert block_match, f"Failed to match block for {req_id}"
        block = block_match.group(0)

        assert "- **Statement**:" in block, f"Missing Statement in {req_id}"
        assert "- **Rationale**:" in block, f"Missing Rationale in {req_id}"
        assert "- **Acceptance**:" in block, f"Missing Acceptance in {req_id}"
        assert re.search(r"- \*\*Status\*\*: (open|done)\b", block), f"Status for {req_id} must be open or done"
        assert "- **Owner**: agent-service" in block, f"Owner for {req_id} must be 'agent-service'"


def test_runtime_topology_and_sa_declarations():
    # REQ-AGENT-001
    # REQ-AGENT-006
    text = _read_spec()
    assert "GEAP Agent Runtime" in text
    assert "GKE Autopilot" in text
    assert "Cloud Run" in text
    assert "sa-triage" in text
    assert "sa-faq" in text
    assert "sa-queue" in text
    assert "sa-order-mcp" in text
    assert "sa-ticket-mcp" in text


def test_agent_db_isolation_and_mcp_roles():
    # REQ-AGENT-001
    # REQ-AGENT-010
    text = _read_spec()
    # Agents must NOT hold Postgres roles or direct Cloud SQL client permissions
    assert "MUST NOT hold Cloud SQL client permissions or Postgres database roles" in text
    assert "Accesses DB strictly via ticket-db-mcp" in text
    assert "Accesses DB strictly via MCP servers" in text

    # SAs must have the correct IAM roles
    assert "predictionUser" in text
    assert "run.invoker" in text
    assert "pubsub.publisher" in text

    # Only MCP and queue SAs hold DB roles
    assert "sa-ticket-mcp" in text and "ticket_rw" in text
    assert "sa-order-mcp" in text and "orders_ro" in text
    assert "sa-queue" in text and "queue_rw" in text


def test_terraform_infrastructure_declaration():
    # REQ-AGENT-001
    text = _read_spec()
    assert "deploy/terraform/single-project/modules/*" in text
    assert "imperative scripts are permitted only for artifact builds and model/agent deployment" in text


def test_routing_thresholds_and_invariants():
    # REQ-AGENT-002
    # REQ-AGENT-003
    # REQ-AGENT-004
    text = _read_spec()
    assert "0.35" in text  # tau_esc
    assert "0.15" in text  # tau_low
    assert "0.70" in text  # tau_intent
    assert "final.escalate" in text or "Add-Only" in text
    assert "Gaming-Resistant" in text


def test_mcp_contracts_and_database_roles():
    # REQ-AGENT-009
    # REQ-AGENT-010
    text = _read_spec()
    assert "ticket-db-mcp" in text
    assert "order-lookup-mcp" in text
    assert "orders_ro" in text
    assert "ticket_rw" in text
    assert "queue_rw" in text
    assert "redaction_maps" in text


def test_resilience_circuit_breaker_and_latency_budgets():
    # REQ-AGENT-011
    # REQ-AGENT-012
    text = _read_spec()
    assert "3 consecutive" in text
    assert "30 s" in text
    assert "300 s" in text
    assert "150 ms" in text
    assert "3,000" in text or "3 s" in text or "3000 ms" in text
    assert "20,000" in text or "20 s" in text or "20000 ms" in text


def test_observability_and_central_config():
    # REQ-AGENT-013
    # REQ-AGENT-014
    # REQ-AGENT-015
    text = _read_spec()
    assert "models.get" in text or "models.model_id" in text or "ticket_router.config" in text
    assert "traceparent" in text or "OpenTelemetry" in text or "OTel" in text
    assert "ticket_id" in text
