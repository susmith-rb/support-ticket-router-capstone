"""Behaviour-Equivalence Replay Test.

# REQ-AGENT-039: Staged Linear Pipeline Architecture and Function LOC Boundaries
# REQ-SAFE-006: Staged Triage Pipeline and Golden Replay Verification Invariant

Replays all 27 canonical scenarios against tests/agents/golden/triage_golden_v1.json
and asserts bit-for-bit behavioral equivalence:
1. TriageResult fields (decision, tier, queue, predicted_intent, escalate, reason, degraded, calming_ack/answer, handoff_response status/route, status_code)
2. MCP calls made (names + sanitized args)
3. Logged structured event names
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.agents.golden.harness import SCENARIO_IDS, run_scenario

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "triage_golden_v1.json"


@pytest.fixture(scope="module")
def golden_data() -> dict[str, Any]:
    with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_triage_behavioral_equivalence_golden_replay(
    scenario_id: str,
    golden_data: dict[str, Any],
) -> None:
    """Verify scenario execution matches recorded golden at HEAD e272c8e."""
    assert scenario_id in golden_data["scenarios"], f"Scenario {scenario_id} missing from golden!"
    expected = golden_data["scenarios"][scenario_id]

    actual = run_scenario(scenario_id)

    assert len(actual["turns"]) == len(expected["turns"]), (
        f"[{scenario_id}] Turn count mismatch: {len(actual['turns'])} != {len(expected['turns'])}"
    )

    for idx, (act_turn, exp_turn) in enumerate(zip(actual["turns"], expected["turns"])):
        # 1. Compare TriageResult fields
        act_res = act_turn["result"]
        exp_res = exp_turn["result"]

        # Decision dict
        assert act_res["decision"] == exp_res["decision"], (
            f"[{scenario_id} Turn {idx}] Decision mismatch: {act_res['decision']} != {exp_res['decision']}"
        )
        # Calming ack
        assert act_res["calming_ack"] == exp_res["calming_ack"], (
            f"[{scenario_id} Turn {idx}] Calming ack mismatch"
        )
        # Answer
        assert act_res["answer"] == exp_res["answer"], (
            f"[{scenario_id} Turn {idx}] Answer mismatch"
        )
        # Status code
        assert act_res["status_code"] == exp_res["status_code"], (
            f"[{scenario_id} Turn {idx}] Status code mismatch: {act_res['status_code']} != {exp_res['status_code']}"
        )
        # Error
        assert act_res["error"] == exp_res["error"], (
            f"[{scenario_id} Turn {idx}] Error mismatch: {act_res['error']} != {exp_res['error']}"
        )
        # Handoff response
        assert act_res["handoff_response"] == exp_res["handoff_response"], (
            f"[{scenario_id} Turn {idx}] Handoff response mismatch"
        )

        # 2. Compare MCP tool calls
        assert act_turn["mcp_calls"] == exp_turn["mcp_calls"], (
            f"[{scenario_id} Turn {idx}] MCP calls mismatch:\nActual: {act_turn['mcp_calls']}\nExpected: {exp_turn['mcp_calls']}"
        )

        # 3. Compare Logged Events
        assert act_turn["logged_events"] == exp_turn["logged_events"], (
            f"[{scenario_id} Turn {idx}] Logged events mismatch:\nActual: {act_turn['logged_events']}\nExpected: {exp_turn['logged_events']}"
        )
