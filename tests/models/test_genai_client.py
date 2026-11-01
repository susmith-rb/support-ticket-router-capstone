"""Unit tests for centralized Google GenAI client factory.

# REQ-AGENT-013: Centralized Model Configuration Resolution
"""

from __future__ import annotations

from unittest.mock import patch

from ticket_router.models.genai_client import get_genai_client


def test_get_genai_client_location_precedence() -> None:
    # models.yaml sets router_tier2 location to us-central1
    with patch("google.genai.Client") as mock_client:
        get_genai_client(role="router_tier2", project="test-project", location="europe-west1")
        mock_client.assert_called_once()
        _, kwargs = mock_client.call_args
        assert kwargs["project"] == "test-project"
        assert kwargs["vertexai"] is True
        # models.yaml location for router_tier2 takes precedence
        assert kwargs["location"] == "global"


def test_get_genai_client_fallback_location() -> None:
    # An unknown role falls back to caller-supplied location or settings
    with patch("google.genai.Client") as mock_client:
        get_genai_client(role="unknown_role", project="test-proj", location="asia-east1")
        mock_client.assert_called_once()
        _, kwargs = mock_client.call_args
        assert kwargs["project"] == "test-proj"
        assert kwargs["location"] == "asia-east1"


def test_get_genai_client_project_from_settings() -> None:
    with patch("google.genai.Client") as mock_client:
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "env-project", "GOOGLE_CLOUD_LOCATION": "us-west1"}):
            get_genai_client(role="unknown_role")
            mock_client.assert_called_once()
            _, kwargs = mock_client.call_args
            assert kwargs["project"] == "env-project"
            assert kwargs["location"] == "us-west1"
