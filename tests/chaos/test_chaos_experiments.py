"""Unit tests for chaos experiments: dry-run defaults, degraded behavior, and restore-in-finally.

# REQ-CHAOS-001: Chaos Engineering Fault Injection, Probing, and Safe Restoration
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chaos.base import ChaosAssertionError, ChaosSafetyError
from chaos.experiment_cloud_sql import CloudSqlChaosExperiment
from chaos.experiment_gemini_429 import Gemini429ChaosExperiment
from chaos.experiment_router_s1 import RouterS1ChaosExperiment


def test_dry_run_defaults_and_safety_gate():
    # REQ-CHAOS-001: Safe dry-run defaults and safety confirmation gates
    exp_s1 = RouterS1ChaosExperiment()
    assert exp_s1.dry_run is True
    assert exp_s1.execute is False
    assert exp_s1.confirm_destructive is False

    exp_429 = Gemini429ChaosExperiment()
    assert exp_429.dry_run is True
    assert exp_429.execute is False

    exp_sql = CloudSqlChaosExperiment()
    assert exp_sql.dry_run is True
    assert exp_sql.execute is False

    # Attempting to execute live without confirm_destructive MUST raise ChaosSafetyError
    unsafe_exp = RouterS1ChaosExperiment(execute=True, confirm_destructive=False)
    with pytest.raises(ChaosSafetyError) as exc_info:
        unsafe_exp.run()
    assert "CHAOS SAFETY GATE" in str(exc_info.value)
    assert "--confirm-destructive" in str(exc_info.value)

    unsafe_sql = CloudSqlChaosExperiment(execute=True, confirm_destructive=False)
    with pytest.raises(ChaosSafetyError):
        unsafe_sql.run()


def test_chaos_experiments_lifecycle_and_restoration(tmp_path: Path):
    # REQ-CHAOS-001: End-to-end dry-run lifecycle and report generation
    # 1. router-s1
    exp_s1 = RouterS1ChaosExperiment(dry_run=True, out_dir=tmp_path)
    report_s1 = exp_s1.run()
    assert report_s1["status"] == "COMPLETED"
    assert report_s1["phases"]["assertion"]["passed"] is True
    assert report_s1["phases"]["restoration"]["simulated_deployed"] is True
    assert Path(report_s1["report_file"]).is_file()

    # 2. Gemini 429
    exp_429 = Gemini429ChaosExperiment(dry_run=True, out_dir=tmp_path)
    report_429 = exp_429.run()
    assert report_429["status"] == "COMPLETED"
    assert report_429["phases"]["assertion"]["passed"] is True
    assert report_429["phases"]["fault_injection"]["code_hook_gap"] is True
    assert report_429["phases"]["restoration"]["simulated_active"] is False
    assert Path(report_429["report_file"]).is_file()

    # 3. Cloud SQL
    exp_sql = CloudSqlChaosExperiment(dry_run=True, out_dir=tmp_path)
    report_sql = exp_sql.run()
    assert report_sql["status"] == "COMPLETED"
    assert report_sql["phases"]["assertion"]["passed"] is True
    assert report_sql["phases"]["assertion"]["agent_failed_open"] is True
    assert report_sql["phases"]["restoration"]["simulated_state"] == "RUNNABLE"
    assert Path(report_sql["report_file"]).is_file()


def test_restore_in_finally_on_probe_crash_router_s1(tmp_path: Path):
    # REQ-CHAOS-001: Guaranteed restoration in finally block even when probe raises exception
    exp = RouterS1ChaosExperiment(dry_run=True, out_dir=tmp_path)

    # Force probe to crash violently
    with patch.object(exp, "run_probe", side_effect=RuntimeError("Simulated probe crash")):
        with patch.object(exp, "restore_fault", wraps=exp.restore_fault) as spy_restore:
            with pytest.raises(RuntimeError) as exc_info:
                exp.run()
            assert "Simulated probe crash" in str(exc_info.value)
            # spy_restore MUST have been called despite exception
            spy_restore.assert_called_once()
            assert exp._simulated_deployed is True


def test_restore_in_finally_on_probe_crash_cloud_sql(tmp_path: Path):
    # REQ-CHAOS-001: Cloud SQL restoration in finally block on probe exception
    exp = CloudSqlChaosExperiment(dry_run=True, out_dir=tmp_path)

    with patch.object(exp, "run_probe", side_effect=RuntimeError("MCP probe network crash")):
        with patch.object(exp, "restore_fault", wraps=exp.restore_fault) as spy_restore:
            with pytest.raises(RuntimeError):
                exp.run()
            spy_restore.assert_called_once()
            assert exp._simulated_db_running is True


def test_restore_in_finally_on_probe_crash_gemini_429(tmp_path: Path):
    # REQ-CHAOS-001: Gemini 429 restoration in finally block on probe exception
    exp = Gemini429ChaosExperiment(dry_run=True, out_dir=tmp_path)

    with patch.object(exp, "run_probe", side_effect=RuntimeError("Generation crash")):
        with patch.object(exp, "restore_fault", wraps=exp.restore_fault) as spy_restore:
            with pytest.raises(RuntimeError):
                exp.run()
            spy_restore.assert_called_once()
            assert exp._fault_active is False


def test_live_restore_in_finally_mocked_subprocess(tmp_path: Path):
    # REQ-CHAOS-001: Subprocess execution and restore verification
    exp = RouterS1ChaosExperiment(
        dry_run=False,
        execute=True,
        confirm_destructive=True,
        out_dir=tmp_path,
    )

    mock_run = MagicMock()
    # Baseline describe returns valid deployed model
    mock_run.return_value.stdout = json.dumps({"deployedModels": [{"id": "dep-12345"}]})

    with patch("subprocess.run", mock_run):
        # Crash probe to trigger finally block
        with patch.object(exp, "run_probe", side_effect=ValueError("Probe timeout")):
            with pytest.raises(ValueError):
                exp.run()

        # Check subprocess calls: undeploy was called, and deploy-model was called in finally
        cmds = [c[0][0] for c in mock_run.call_args_list]
        undeploy_called = any("undeploy-model" in cmd for cmd in cmds)
        deploy_called = any("deploy-model" in cmd for cmd in cmds)

        assert undeploy_called, "gcloud undeploy-model was not called"
        assert deploy_called, "gcloud deploy-model was not called in finally block!"


def test_degraded_behavior_assertions():
    # REQ-CHAOS-001: Strict assertion of documented degraded behaviors
    exp_s1 = RouterS1ChaosExperiment()

    # Success case: degraded=True or tier=tier2_flash
    valid_probe_s1 = {
        "turn_completed": True,
        "decision": "auto_rag",
        "tier": "tier2_flash",
        "degraded": True,
    }
    assert exp_s1.assert_degraded_behavior(valid_probe_s1)["passed"] is True

    # Failure case: turn failed
    with pytest.raises(ChaosAssertionError):
        exp_s1.assert_degraded_behavior({"turn_completed": False, "error": "Internal Error"})

    # Failure case: tier remained tier1 without fallback
    with pytest.raises(ChaosAssertionError):
        exp_s1.assert_degraded_behavior({"turn_completed": True, "tier": "tier1_router_s1", "degraded": False})

    # Gemini 429 assertions
    exp_429 = Gemini429ChaosExperiment()
    valid_probe_429 = {
        "turn_completed": True,
        "decision": "escalate",
        "assigned_queue": "human-standard",
        "degraded": True,
        "calming_ack_sent": True,
    }
    assert exp_429.assert_degraded_behavior(valid_probe_429)["passed"] is True

    # Failure: didn't escalate
    with pytest.raises(ChaosAssertionError):
        exp_429.assert_degraded_behavior({
            "turn_completed": True,
            "decision": "auto_rag",
            "degraded": False,
            "calming_ack_sent": False,
        })

    # Failure: missing calming ack
    with pytest.raises(ChaosAssertionError):
        exp_429.assert_degraded_behavior({
            "turn_completed": True,
            "decision": "escalate",
            "degraded": True,
            "calming_ack_sent": False,
        })

    # Cloud SQL assertions
    exp_sql = CloudSqlChaosExperiment()
    valid_probe_sql = {
        "mcp_status_code": 503,
        "mcp_failed_fast": True,
        "turn_completed": True,
        "agent_failed_open": True,
    }
    assert exp_sql.assert_degraded_behavior(valid_probe_sql)["passed"] is True

    # Failure: MCP did not fail fast
    with pytest.raises(ChaosAssertionError):
        exp_sql.assert_degraded_behavior({
            "mcp_status_code": 200,
            "mcp_failed_fast": False,
            "turn_completed": True,
            "agent_failed_open": True,
        })

    # Failure: agent crashed
    with pytest.raises(ChaosAssertionError):
        exp_sql.assert_degraded_behavior({
            "mcp_status_code": 503,
            "mcp_failed_fast": True,
            "turn_completed": False,
            "agent_failed_open": False,
        })
