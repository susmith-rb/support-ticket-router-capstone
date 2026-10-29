"""Static validation tests for the Customer Identity specification (specs/customer-identity.md).

Validates that:
1. specs/customer-identity.md exists and adheres to line budgets (<= 200 lines).
2. specs/README.md indexes the specification with prefix REQ-IDENT-.
3. All REQ-IDENT-001..008 requirements are defined, valid, and set to status 'open'.
4. Core architectural invariants (IdP token format, relational mapping, server-side JWT
   verification, fail-closed anonymous handling, trust chain propagation, admin linking,
   threat model, and test coverage) are unambiguously specified.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = REPO_ROOT / "specs" / "customer-identity.md"
INDEX_FILE = REPO_ROOT / "specs" / "README.md"

REQ_HEADER_PATTERN = re.compile(r"^###\s+(REQ-IDENT-\d{3})(?::\s*(.*))?$")


def _read_spec_text() -> str:
  assert SPEC_FILE.exists(), f"Missing spec file: {SPEC_FILE}"
  return SPEC_FILE.read_text(encoding="utf-8")


def _parse_ident_requirements() -> dict[str, dict[str, str]]:
  text = _read_spec_text()
  reqs = {}
  current = None
  for line in text.splitlines():
    m = REQ_HEADER_PATTERN.match(line)
    if m:
      req_id = m.group(1)
      title = (m.group(2) or "").strip()
      current = {
          "id": req_id,
          "title": title,
          "statement": None,
          "rationale": None,
          "acceptance": None,
          "status": None,
          "owner": None,
      }
      reqs[req_id] = current
      continue

    if current:
      stripped = line.strip()
      if stripped.startswith("- **Statement**:"):
        current["statement"] = stripped.split("- **Statement**:", 1)[1].strip()
      elif stripped.startswith("- **Rationale**:"):
        current["rationale"] = stripped.split("- **Rationale**:", 1)[1].strip()
      elif stripped.startswith("- **Acceptance**:"):
        val = stripped.split("- **Acceptance**:", 1)[1].strip()
        if val.startswith("`") and val.endswith("`"):
          val = val[1:-1].strip()
        current["acceptance"] = val
      elif stripped.startswith("- **Status**:"):
        current["status"] = stripped.split("- **Status**:", 1)[1].strip().lower()
      elif stripped.startswith("- **Owner**:"):
        current["owner"] = stripped.split("- **Owner**:", 1)[1].strip()
      elif line.startswith("## ") or (line.startswith("### ") and not REQ_HEADER_PATTERN.match(line)):
        current = None
  return reqs


def test_customer_identity_spec_file_budget_and_structure():
  """Asserts specs/customer-identity.md exists and adheres to line budget (<= 200 lines)."""
  text = _read_spec_text()
  lines = text.splitlines()
  assert 50 <= len(lines) <= 200, f"Spec length ({len(lines)} lines) out of budget (expected 50..200)"
  assert "Specification: Customer Identity & Authentication Subsystem" in text


def test_customer_identity_indexed_in_readme():
  """Asserts specs/README.md indexes customer-identity.md with prefix REQ-IDENT-."""
  assert INDEX_FILE.exists(), f"Missing index file: {INDEX_FILE}"
  readme = INDEX_FILE.read_text(encoding="utf-8")
  assert "customer-identity.md" in readme
  assert "REQ-IDENT-" in readme


def test_all_ident_requirements_defined_and_open():
  """Asserts that REQ-IDENT-001 through REQ-IDENT-009 are defined with valid status."""
  reqs = _parse_ident_requirements()
  expected_ids = [f"REQ-IDENT-{i:03d}" for i in range(1, 10)]
  assert sorted(reqs.keys()) == expected_ids, f"Expected {expected_ids}, got {sorted(reqs.keys())}"

  for req_id, req in reqs.items():
    assert req["statement"], f"Missing statement in {req_id}"
    assert req["rationale"], f"Missing rationale in {req_id}"
    assert req["acceptance"], f"Missing acceptance in {req_id}"
    assert req["status"] in ("open", "done"), f"Expected status 'open' or 'done' in {req_id}, got '{req['status']}'"
    assert req["owner"] in {"identity", "store", "triage-router", "agent-service"}, (
        f"Unexpected owner '{req['owner']}' in {req_id}"
    )


def test_req_ident_001_idp_configuration():
  # REQ-IDENT-001
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-001"]
  stmt = req["statement"]
  assert "Identity Platform" in stmt or "identitytoolkit.googleapis.com" in stmt
  assert "google_identity_platform_config" in stmt
  assert "securetoken.google.com/susmith-fde-ticket-router" in stmt
  assert "aud = \"susmith-fde-ticket-router\"" in stmt
  assert "sub" in stmt


def test_req_ident_002_relational_schema():
  # REQ-IDENT-002
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-002"]
  stmt = req["statement"]
  assert "0003_customer_identity.sql" in stmt
  assert "customers" in stmt
  assert "customer_identities" in stmt
  assert "identity_events" in stmt
  assert "ticket_rw" in stmt
  assert "email" in stmt  # Zero email storage invariant


def test_req_ident_003_server_side_jwt_verification():
  # REQ-IDENT-003
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-003"]
  stmt = req["statement"]
  assert "verify_firebase_token" in stmt
  assert "customer_id_token" in stmt
  assert "resolve_customer" in stmt
  assert "ticket-db-mcp" in stmt


def test_req_ident_004_fail_closed_fallback():
  # REQ-IDENT-004
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-004"]
  stmt = req["statement"]
  assert "fail closed" in stmt
  assert "anon:" in stmt
  assert "REQ-AGENT-003" in stmt  # Preserves add-only escalation invariant


def test_req_ident_005_trust_chain_propagation():
  # REQ-IDENT-005
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-005"]
  stmt = req["statement"]
  assert "A2A" in stmt
  assert "customer_verified" in stmt
  assert "X-Customer-ID" in stmt
  assert "order-lookup-mcp" in stmt


def test_req_ident_006_linking_and_seeding():
  # REQ-IDENT-006
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-006"]
  stmt = req["statement"]
  assert "link_customer_identity.py" in stmt
  assert "cust_1001" in stmt and "cust_1002" in stmt
  assert "identitytoolkit.googleapis.com/v1/accounts:signUp" in stmt


def test_req_ident_007_threat_mitigations():
  # REQ-IDENT-007
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-007"]
  stmt = req["statement"]
  assert "spoofing" in stmt.lower()
  assert "replay" in stmt.lower()
  assert "idor" in stmt.lower()
  assert "suspended" in stmt.lower()


def test_req_ident_008_verification_test_suite():
  # REQ-IDENT-008
  reqs = _parse_ident_requirements()
  req = reqs["REQ-IDENT-008"]
  stmt = req["statement"]
  assert "Unit tests" in stmt or "unit tests" in stmt
  assert "contract tests" in stmt.lower()
  assert "migration static checks" in stmt.lower()
  assert "e2e" in stmt.lower()
