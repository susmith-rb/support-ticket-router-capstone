"""Static validation tests for the End-to-End Agent Evaluation & Hill-Climbing specification.

Validates that:
1. specs/agent-evals.md exists and adheres to line budget (<= 220 lines).
2. specs/README.md indexes the specification with prefix REQ-EVAL-.
3. All REQ-EVAL-001..010 requirements are defined, unique, have status 'open', and owner 'agent-evals'.
4. Core architectural invariants are specified:
   - Datasets: multi-source builder, offline-authored adversarial tone set, strict DEV/TEST isolation.
   - Splits: router golden set 150 DEV / 150 TEST by strata; adversarial tone set split by seed (20/20).
   - Runner: dual-target (local and deployed Agent Engine), parallelism, retries, $5.00 cost cap.
   - Deterministic metrics: hard gates (recall >= 0.98 + Wilson LB, BOLA=0, gaming=0, route latencies),
     provisional targets (routing acc, false-esc, auto-res, citation valid, cost/tkt), scorecard notes.
   - LLM-as-a-judge: anchored 5/3/1 rubrics, judge packet export, offline Jetski execution, ingest, calibration.
   - Hill-climbing protocol: versioned agent_tuning.yaml + prompts/*.yaml, coordinate ascent on DEV only,
     live agent coupling with default zero-change behavior, audit log + README table, test_runs.log audit gate.
   - CI: 20-case deterministic smoke gate behind change detection.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = REPO_ROOT / "specs" / "agent-evals.md"
INDEX_FILE = REPO_ROOT / "specs" / "README.md"

REQ_HEADER_PATTERN = re.compile(r"^###\s+(REQ-EVAL-\d{3})(?::\s*(.*))?$")
EXPECTED_REQS = [f"REQ-EVAL-{i:03d}" for i in range(1, 11)]


def _read_spec_text() -> str:
    assert SPEC_FILE.exists(), f"Missing spec file: {SPEC_FILE}"
    return SPEC_FILE.read_text(encoding="utf-8")


def _parse_eval_requirements() -> dict[str, dict[str, str]]:
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


def test_agent_evals_spec_file_budget_and_structure():
    # REQ-EVAL-001
    text = _read_spec_text()
    lines = text.splitlines()
    assert 60 <= len(lines) <= 220, f"Spec length ({len(lines)} lines) out of budget (expected 60..220)"
    assert "Specification: End-to-End Agent Evaluation & Hill-Climbing System" in text


def test_agent_evals_indexed_in_readme():
    # REQ-EVAL-001
    assert INDEX_FILE.exists(), f"Missing index file: {INDEX_FILE}"
    readme = INDEX_FILE.read_text(encoding="utf-8")
    assert "agent-evals.md" in readme
    assert "REQ-EVAL-" in readme


def test_all_eval_requirements_defined_and_open():
    # REQ-EVAL-001
    # REQ-EVAL-002
    # REQ-EVAL-003
    # REQ-EVAL-004
    # REQ-EVAL-005
    # REQ-EVAL-006
    # REQ-EVAL-007
    # REQ-EVAL-008
    # REQ-EVAL-009
    # REQ-EVAL-010
    reqs = _parse_eval_requirements()
    assert sorted(reqs.keys()) == EXPECTED_REQS, f"Expected {EXPECTED_REQS}, got {sorted(reqs.keys())}"

    for req_id in EXPECTED_REQS:
        req = reqs[req_id]
        assert req["statement"], f"Missing statement in {req_id}"
        assert req["rationale"], f"Missing rationale in {req_id}"
        assert req["acceptance"], f"Missing acceptance in {req_id}"
        assert req["status"] == "open", f"Expected status 'open' in {req_id}, got '{req['status']}'"
        assert req["owner"] == "agent-evals", f"Expected owner 'agent-evals' in {req_id}, got '{req['owner']}'"


def test_dataset_and_adversarial_requirements():
    # REQ-EVAL-001
    # REQ-EVAL-002
    # REQ-EVAL-003
    text = _read_spec_text()
    assert "evals/build_agent_e2e.py" in text
    assert "evals/data/agent_e2e_v1" in text
    assert "evals/data/adversarial_tone_v1.jsonl" in text
    assert "labels_C2.jsonl" in text
    assert "golden_all_model_outputs.csv" in text
    assert "demo users 1001 and 1002" in text
    assert "150 DEV and 150 TEST" in text
    assert "Split BY SEED" in text or "split strictly by seed" in text.lower()
    assert "calm" in text and "fake-urgent" in text and "angry" in text and "abusive" in text
    assert "Jetski agents" in text
    assert "never Vertex AI billed to Argolis" in text
    assert "zero leakage between DEV and TEST" in text


def test_runner_and_telemetry_requirements():
    # REQ-EVAL-004
    # REQ-EVAL-005
    text = _read_spec_text()
    assert "evals/agent_e2e.py" in text
    assert "local" in text and "deployed" in text
    assert "create_session" in text and "stream_query" in text
    assert "5.00" in text  # $5.00 cost cap
    assert "case_id" in text and "route_decision" in text and "tiers_used" in text
    assert "assigned_queue" in text and "ticket_id" in text and "citations" in text
    assert "customer_verified" in text and "latencies" in text


def test_deterministic_metrics_and_latency_gates():
    # REQ-EVAL-006
    text = _read_spec_text()
    assert "evals/agent_metrics.py" in text
    assert "HARD GATES" in text
    assert "0.98" in text
    assert "Wilson" in text
    assert "BOLA" in text
    assert "priority-gaming" in text
    assert "p95 <= 3.0 s" in text or "Esc <= 3s" in text
    assert "p95 <= 20.0 s" in text or "FAQ <= 20s" in text
    assert "PROVISIONAL TARGETS" in text
    assert "teacher/LLM labels" in text


def test_llm_judge_and_offline_rubric_requirements():
    # REQ-EVAL-007
    text = _read_spec_text()
    assert "5/3/1" in text
    assert "Groundedness" in text
    assert "Helpfulness/Empathy" in text
    assert "Calming Ack Tone" in text
    assert "packets_" in text
    assert "evals/agent_judge_ingest.py" in text
    assert "calibration" in text
    assert "kappa" in text
    assert "Vertex Gen AI Evaluation Service" in text


def test_hill_climb_and_audit_gate_requirements():
    # REQ-EVAL-008
    # REQ-EVAL-009
    text = _read_spec_text()
    assert "agent_tuning.yaml" in text
    assert "prompts/*.yaml" in text
    assert "DEV only" in text
    assert "tau_esc" in text and "tau_low" in text and "tau_intent" in text
    assert "auto_resolution" in text and "mean_groundedness" in text
    assert "evals/hill_climb/log.jsonl" in text
    assert "evals/results/test_runs.log" in text
    assert "pre-registered" in text


def test_ci_smoke_gate_requirements():
    # REQ-EVAL-010
    text = _read_spec_text()
    assert "20-case" in text
    assert "smoke" in text
    assert "change detection" in text
    assert "zero external network" in text or "hermetic" in text
