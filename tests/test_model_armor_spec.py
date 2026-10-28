"""Static validation tests for the Model Armor specification (specs/model-armor.md).

# REQ-MA-001: Declarative Terraform Infrastructure, Template & Least-Privilege IAM
# REQ-MA-002: Regional Keep-Alive REST Client, Typed Verdicts & Measured Timeout
# REQ-MA-003: Ingress Concurrent Evaluation, Security Policy & Fail-Safe Escalation
# REQ-MA-004: Egress Sanitization for FaqResolver Answers & Fallback Handoff
# REQ-MA-005: In-Region Latency Budget, Concurrent SLA & Degraded Metrics
# REQ-MA-006: Verification Test Strategy: Fakes, Contracts, Static TF & Live Smoke

Validates that:
1. specs/model-armor.md exists and adheres to line budget (<= 160 lines).
2. specs/README.md indexes the specification with prefix REQ-MA-.
3. All REQ-MA-001..006 requirements are defined, valid, and set to status 'done' or 'open'.
4. Core architectural invariants (Terraform template, regional keep-alive client,
   concurrent ingress security, fail-safe degradation, egress answer sanitization,
   latency budget, degraded metrics, and verification strategy) are specified.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = REPO_ROOT / "specs" / "model-armor.md"
INDEX_FILE = REPO_ROOT / "specs" / "README.md"

REQ_HEADER_PATTERN = re.compile(r"^###\s+(REQ-MA-\d{3})(?::\s*(.*))?$")
EXPECTED_REQ_IDS = [f"REQ-MA-{i:03d}" for i in range(1, 7)]


def _read_spec_text() -> str:
    assert SPEC_FILE.exists(), f"Missing spec file: {SPEC_FILE}"
    return SPEC_FILE.read_text(encoding="utf-8")


def _parse_ma_requirements() -> dict[str, dict[str, str]]:
    text = _read_spec_text()
    reqs: dict[str, dict[str, str]] = {}
    current: dict[str, str | None] | None = None
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
            reqs[req_id] = current  # type: ignore[assignment]
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
    return reqs  # type: ignore[return-value]


def test_model_armor_spec_file_budget_and_structure():
    # REQ-MA-001
    text = _read_spec_text()
    lines = text.splitlines()
    assert 50 <= len(lines) <= 160, f"Spec length ({len(lines)} lines) out of budget (expected 50..160)"
    assert "Specification: Model Armor Security & Sanitization Subsystem" in text


def test_model_armor_indexed_in_readme():
    # REQ-MA-001
    assert INDEX_FILE.exists(), f"Missing index file: {INDEX_FILE}"
    readme = INDEX_FILE.read_text(encoding="utf-8")
    assert "model-armor.md" in readme
    assert "REQ-MA-" in readme


def test_all_ma_requirements_defined_and_valid():
    # REQ-MA-001
    # REQ-MA-002
    # REQ-MA-003
    # REQ-MA-004
    # REQ-MA-005
    # REQ-MA-006
    reqs = _parse_ma_requirements()
    assert sorted(reqs.keys()) == EXPECTED_REQ_IDS, f"Expected {EXPECTED_REQ_IDS}, got {sorted(reqs.keys())}"

    allowed_owners = {"infra", "security", "triage-router", "faq-resolver"}
    for req_id, req in reqs.items():
        assert req["statement"], f"Missing statement in {req_id}"
        assert req["rationale"], f"Missing rationale in {req_id}"
        assert req["acceptance"], f"Missing acceptance in {req_id}"
        assert req["status"] in ("open", "done"), f"Expected status 'open' or 'done' in {req_id}, got '{req['status']}'"
        assert req["owner"] in allowed_owners, f"Unexpected owner '{req['owner']}' in {req_id}"


def test_req_ma_001_terraform_infra_and_iam():
    # REQ-MA-001
    req = _parse_ma_requirements()["REQ-MA-001"]
    stmt = req["statement"]
    assert "modelarmor.googleapis.com" in stmt
    assert "google_model_armor_template" in stmt
    assert "support-router-lean" in stmt
    assert "us-central1" in stmt
    assert "pi_and_jailbreak_filter_settings" in stmt
    assert "MEDIUM_AND_ABOVE" in stmt
    assert "malicious_uri_filter_settings" in stmt
    assert "rai_settings" in stmt
    assert "SEXUALLY_EXPLICIT" in stmt
    assert "HATE_SPEECH" in stmt
    assert "HARASSMENT" in stmt
    assert "DANGEROUS" in stmt
    assert "sdp_settings" in stmt
    assert "basic_config" in stmt
    assert "ORD-10001" in stmt
    assert "[ORDER_ID]" in stmt
    assert "roles/modelarmor.user" in stmt
    assert "sa-triage@" in stmt
    assert "sa-faq@" in stmt


def test_req_ma_002_regional_client_and_resilience():
    # REQ-MA-002
    req = _parse_ma_requirements()["REQ-MA-002"]
    stmt = req["statement"]
    assert "https://modelarmor.us-central1.rep.googleapis.com" in stmt
    assert "requests.Session" in stmt or "keep-alive" in stmt
    assert "HTTPAdapter" in stmt
    assert "ticket_router.common.auth.get_access_token" in stmt
    assert "sanitize_user_prompt" in stmt
    assert "sanitize_model_response" in stmt
    assert "is_clean" in stmt
    assert "blocked" in stmt
    assert "CircuitBreaker" in stmt
    assert "model_armor_degraded" in stmt


def test_req_ma_003_ingress_policy_and_fail_safe():
    # REQ-MA-003
    req = _parse_ma_requirements()["REQ-MA-003"]
    stmt = req["statement"]
    assert "TriageRouterAgent" in stmt
    assert "CONCURRENTLY" in stmt
    assert "router-s1" in stmt
    assert "pi_jb" in stmt
    assert "malicious URIs" in stmt or "malicious_uri" in stmt
    assert "NEVER" in stmt
    assert "human queue" in stmt
    assert "harassment/abuse" in stmt
    assert "boundary acknowledgement" in stmt
    assert "REQ-AGENT-004" in stmt
    assert "body_redacted" in stmt
    assert "fail safe" in stmt
    assert "degraded = True" in stmt
    assert "REQ-AGENT-014" in stmt


def test_req_ma_004_egress_sanitization():
    # REQ-MA-004
    req = _parse_ma_requirements()["REQ-MA-004"]
    stmt = req["statement"]
    assert "FaqResolverAgent" in stmt
    assert "sanitize_model_response" in stmt
    assert "ESCALATED" in stmt
    assert "human-standard" in stmt
    assert "model_armor_response_blocked" in stmt
    assert "degraded = True" in stmt


def test_req_ma_005_latency_budget_and_concurrent_sla():
    # REQ-MA-005
    req = _parse_ma_requirements()["REQ-MA-005"]
    stmt = req["statement"]
    assert "300 ms" in stmt or "300" in stmt
    assert "us-central1" in stmt
    assert "Concurrent execution" in stmt or "CONCURRENTLY" in req["statement"] or "concurrent" in req["statement"].lower()
    assert "laya-tune" in stmt
    assert "model_armor_degraded" in stmt


def test_req_ma_006_verification_test_strategy():
    # REQ-MA-006
    req = _parse_ma_requirements()["REQ-MA-006"]
    stmt = req["statement"]
    assert "FakeModelArmorClient" in stmt
    assert "Contract tests" in stmt or "contract tests" in stmt
    assert "Static Terraform tests" in stmt or "static Terraform tests" in stmt
    assert "scripts/model_armor_smoke.py" in stmt
    assert "--latency" in stmt
