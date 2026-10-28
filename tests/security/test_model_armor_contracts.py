"""Contract and smoke harness verification tests for Model Armor.

# REQ-MA-006: Verification Test Strategy: Fakes, Contracts, Static TF & Live Smoke
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.model_armor_smoke import run_functional_smoke, run_latency_benchmark
from ticket_router.security.model_armor import (
    DEFAULT_MODEL_ARMOR_BASE_URL,
    FakeModelArmorClient,
    ModelArmorClient,
    ModelArmorVerdict,
    parse_model_armor_response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "model_armor_smoke.py"


def test_contracts_and_smoke_harness():
    """Verify REST API contracts, JSON schema serialization, and live smoke test harness."""
    # 1. REST Request Serialization Contracts
    template = "projects/susmith-fde-ticket-router/locations/us-central1/templates/support-router-lean"
    client = ModelArmorClient(template=template, token_provider=lambda: "mock-token")

    # Intercept HTTP session to inspect outgoing request payload and URL
    mock_post = MagicMock()
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "sanitizationResult": {
            "filterMatchState": "NO_MATCH_FOUND",
            "filterResults": {},
        }
    }

    with patch.object(client.session, "post", mock_post):
        # A. sanitizeUserPrompt contract
        client.sanitize_user_prompt("Inbound test ticket prompt")
        assert mock_post.called
        args, kwargs = mock_post.call_args
        called_url = args[0]
        called_json = kwargs.get("json")

        assert called_url == f"{DEFAULT_MODEL_ARMOR_BASE_URL}/{template}:sanitizeUserPrompt"
        assert called_json == {"user_prompt_data": {"text": "Inbound test ticket prompt"}}
        assert "Authorization" in kwargs.get("headers", {})

        # B. sanitizeModelResponse contract
        mock_post.reset_mock()
        client.sanitize_model_response("Outbound synthesized answer")
        assert mock_post.called
        args_resp, kwargs_resp = mock_post.call_args
        called_resp_url = args_resp[0]
        called_resp_json = kwargs_resp.get("json")

        assert called_resp_url == f"{DEFAULT_MODEL_ARMOR_BASE_URL}/{template}:sanitizeModelResponse"
        assert called_resp_json == {"model_response_data": {"text": "Outbound synthesized answer"}}

    # 2. REST Response Schema Contract Parsing (Full Multi-Detector Payload)
    full_payload = {
        "sanitizationResult": {
            "filterMatchState": "MATCH_FOUND",
            "sanitizedItem": {
                "text": "Order ORD-10001 with card [CREDIT_CARD_NUMBER]."
            },
            "filterResults": {
                "piAndJailbreakFilterResult": {
                    "executionState": "EXECUTION_SUCCESS",
                    "matchState": "NO_MATCH_FOUND",
                },
                "maliciousUriFilterResult": {
                    "executionState": "EXECUTION_SUCCESS",
                    "matchState": "NO_MATCH_FOUND",
                },
                "raiFilterResult": {
                    "executionState": "EXECUTION_SUCCESS",
                    "raiFilterTypeResults": {
                        "SEXUALLY_EXPLICIT": {
                            "executionState": "EXECUTION_SUCCESS",
                            "matchState": "NO_MATCH_FOUND",
                        },
                        "HATE_SPEECH": {
                            "executionState": "EXECUTION_SUCCESS",
                            "matchState": "NO_MATCH_FOUND",
                        },
                        "HARASSMENT": {
                            "executionState": "EXECUTION_SUCCESS",
                            "matchState": "NO_MATCH_FOUND",
                        },
                        "DANGEROUS": {
                            "executionState": "EXECUTION_SUCCESS",
                            "matchState": "NO_MATCH_FOUND",
                        },
                    },
                },
                "sdpFilterResult": {
                    "executionState": "EXECUTION_SUCCESS",
                    "sdpInspectionResult": {
                        "findings": [
                            {
                                "infoType": "CREDIT_CARD_NUMBER",
                                "likelihood": "VERY_LIKELY",
                            }
                        ]
                    },
                },
            },
        }
    }
    verdict: ModelArmorVerdict = parse_model_armor_response(full_payload, latency_ms=42.5)

    assert verdict.is_clean is False
    assert verdict.blocked is False
    assert verdict.action == "ALLOW"
    assert verdict.has_pi_jb is False
    assert verdict.has_malicious_uri is False
    assert verdict.has_rai_violation is False
    assert verdict.has_sdp_findings is True
    assert len(verdict.sdp_findings) == 1
    assert verdict.sdp_findings[0].info_type == "CREDIT_CARD_NUMBER"
    assert verdict.sanitized_text == "Order ORD-10001 with card [CREDIT_CARD_NUMBER]."
    assert verdict.latency_ms == 42.5

    # 3. Live Smoke Harness Execution with Fake Client
    smoke_fake = FakeModelArmorClient()

    # Functional smoke harness: all 5 checks must pass
    ok_functional = run_functional_smoke(smoke_fake)
    assert ok_functional is True

    # Latency benchmark harness: 5 iterations
    ok_latency = run_latency_benchmark(smoke_fake, n_calls=5)
    assert ok_latency is True

    # 4. Smoke Script Executable & Argument Parsing Contract
    assert SMOKE_SCRIPT.is_file(), f"Missing smoke script: {SMOKE_SCRIPT}"
    assert oct(SMOKE_SCRIPT.stat().st_mode)[-3:] in ("755", "775", "777"), "Script not executable"

    proc = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--template" in proc.stdout
    assert "--latency" in proc.stdout
    assert "--n-calls" in proc.stdout
