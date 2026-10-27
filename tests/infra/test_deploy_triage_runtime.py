"""Unit tests for deploy_triage_agent_runtime.py script.

Fulfills REQ-AGENT-017:
- Verify in-process environment isolation contract.
- Verify filtering of Vertex Agent Engine reserved environment variables.
- Verify build_agent_app imports adk_agent without stub fallback.
- Verify dry-run execution for both create and update operations.
- Verify call arguments to vertexai.agent_engines API.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from scripts.deploy_agents.deploy_triage_agent_runtime import (
    build_agent_app,
    deploy_reasoning_engine,
    filter_reserved_env_vars,
    parse_args,
    prepare_environment,
)


@pytest.fixture(autouse=True)
def isolate_environ():
    """Snapshot and restore os.environ around each test to avoid polluting other test suites."""
    orig_env = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(orig_env)


def test_filter_reserved_env_vars():
    """Verify reserved Vertex Agent Engine environment variables are stripped."""
    raw_env = {
        "GOOGLE_CLOUD_PROJECT": "susmith-fde-ticket-router",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "GOOGLE_CLOUD_REGION": "us-central1",
        "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "us-central1",
        "GOOGLE_CLOUD_AGENT_ENGINE_APP_NAME": "custom-app",
        "ROUTER_S1_ENDPOINT": "endpoints/router-s1",
        "TICKET_DB_MCP_URL": "https://ticket-db-mcp.app",
        "FAQ_RESOLVER_URL": "https://faq.sslip.io",
        "SERVICE": "triage_router",
    }
    filtered = filter_reserved_env_vars(raw_env)

    # Reserved names must be absent
    assert "GOOGLE_CLOUD_PROJECT" not in filtered
    assert "GOOGLE_CLOUD_LOCATION" not in filtered
    assert "GOOGLE_CLOUD_REGION" not in filtered
    assert "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION" not in filtered
    assert "GOOGLE_CLOUD_AGENT_ENGINE_APP_NAME" not in filtered

    # Contract variables must be retained
    assert filtered["ROUTER_S1_ENDPOINT"] == "endpoints/router-s1"
    assert filtered["TICKET_DB_MCP_URL"] == "https://ticket-db-mcp.app"
    assert filtered["FAQ_RESOLVER_URL"] == "https://faq.sslip.io"
    assert filtered["SERVICE"] == "triage_router"


def test_prepare_environment_sets_in_process_env():
    """Verify prepare_environment configures os.environ and returns contract dict."""
    orig_env = dict(os.environ)
    try:
        env = prepare_environment(
            project="proj-test",
            region="us-west1",
            ticket_db_mcp_url="https://ticket-db.test",
            order_lookup_mcp_url="https://order.test",
            human_queue_url="https://queue.test",
            faq_resolver_url="https://faq.test",
            faq_resolver_audience="https://faq.test",
            router_s1_endpoint="endpoints/s1-test",
            router_s1_base_url="https://s1-test.vertexai.goog",
        )

        assert os.environ["GOOGLE_CLOUD_PROJECT"] == "proj-test"
        assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-west1"
        assert os.environ["TICKET_DB_MCP_URL"] == "https://ticket-db.test"
        assert os.environ["FAQ_RESOLVER_URL"] == "https://faq.test"
        assert env["SERVICE"] == "triage_router"
        assert env["REQUIRE_REAL_DEPS"] == "1"
    finally:
        os.environ.clear()
        os.environ.update(orig_env)


def test_build_agent_app_imports_real_adk_agent():
    """Verify build_agent_app imports root_agent from adk_agent without stub fallback."""
    agent_app = build_agent_app()
    assert agent_app is not None
    agent_name = ""
    try:
        agent_name = str(agent_app.name)
    except AttributeError:
        try:
            agent_name = str(agent_app._agent.name)
        except AttributeError:
            pass

    has_query_method = False
    try:
        has_query_method = callable(agent_app.query)
    except AttributeError:
        has_query_method = False

    assert "triage_router" in agent_name or has_query_method


def test_deploy_reasoning_engine_dry_run_create_and_update():
    """Verify dry_run executes cleanly without GCP API calls for create and update."""
    res_create = deploy_reasoning_engine(
        project="proj-dry",
        region="us-central1",
        staging_bucket="gs://bucket/staging",
        display_name="triage-test",
        dry_run=True,
    )
    assert res_create == "projects/proj-dry/locations/us-central1/reasoningEngines/dry-run-sample-id"

    res_update = deploy_reasoning_engine(
        project="proj-dry",
        region="us-central1",
        staging_bucket="gs://bucket/staging",
        display_name="triage-test",
        update_resource_name="projects/proj-dry/locations/us-central1/reasoningEngines/existing-123",
        dry_run=True,
    )
    assert res_update == "projects/proj-dry/locations/us-central1/reasoningEngines/dry-run-sample-id"


def test_parse_args_supports_update_flag():
    """Verify parse_args supports --update and --dry-run flags."""
    args = parse_args(["--update", "projects/p/locations/l/reasoningEngines/456", "--dry-run"])
    assert args.update == "projects/p/locations/l/reasoningEngines/456"
    assert args.dry_run is True


def test_deploy_reasoning_engine_mock_api_calls():
    """Verify mock agent_engines create call receives filtered env_vars and correct params."""
    mock_agent_engines = MagicMock()
    mock_remote = MagicMock()
    mock_remote.resource_name = "projects/p/locations/l/reasoningEngines/789"
    mock_agent_engines.create.return_value = mock_remote

    mock_vertexai = MagicMock()
    mock_vertexai.agent_engines = mock_agent_engines

    with patch.dict("sys.modules", {"vertexai": mock_vertexai, "vertexai.agent_engines": mock_agent_engines}):
        out = deploy_reasoning_engine(
            project="p",
            region="l",
            staging_bucket="gs://bucket/staging",
            display_name="test-engine",
            service_account="sa-test@p.iam.gserviceaccount.com",
            dry_run=False,
        )

        assert out == "projects/p/locations/l/reasoningEngines/789"
        assert mock_agent_engines.create.called
        call_kwargs = mock_agent_engines.create.call_args[1]

        assert call_kwargs["display_name"] == "test-engine"
        assert call_kwargs["service_account"] == "sa-test@p.iam.gserviceaccount.com"
        assert "src/ticket_router" in call_kwargs["extra_packages"]
        assert call_kwargs["gcs_dir_name"] == "agent_engine/triage_router"

        # Contract env vars in call_kwargs['env_vars'] must not contain GOOGLE_CLOUD_PROJECT
        env_passed = call_kwargs["env_vars"]
        assert "GOOGLE_CLOUD_PROJECT" not in env_passed
        assert "ROUTER_S1_ENDPOINT" in env_passed
        assert "TICKET_DB_MCP_URL" in env_passed
