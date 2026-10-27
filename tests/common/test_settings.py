"""Hermetic tests for Settings configuration and validation.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-013: Centralized Model Configuration Resolution
"""

import pytest

from ticket_router.common.settings import Settings


def test_settings_defaults_loaded():
    """Verify default fallback values when env is empty."""
    cfg = Settings.from_env(env={})
    assert cfg.project == "susmith-fde-ticket-router"
    assert cfg.location == "us-central1"
    assert cfg.agent_search_location == "global"
    assert cfg.agent_search_engine_id == "support-kb-search"
    assert "router-s1" in cfg.router_s1_endpoint
    # In pytest test runner, defaults to False for hermetic safety
    assert cfg.require_real_deps is False
    assert cfg.allowed_callers == ()
    assert cfg.order_lookup_mcp_url is None
    assert cfg.ticket_db_mcp_url is None
    assert cfg.faq_resolver_url is None


def test_settings_container_default_requires_real_deps(monkeypatch):
    """Verify in non-test container environments, default is require_real_deps=True."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    cfg = Settings.from_env(env={})
    assert cfg.require_real_deps is True


def test_settings_custom_env_parsing():
    """Verify custom environment variables are parsed correctly."""
    custom_env = {
        "GOOGLE_CLOUD_PROJECT": "test-project-123",
        "GOOGLE_CLOUD_LOCATION": "europe-west1",
        "AGENT_SEARCH_LOCATION": "us-central1",
        "AGENT_SEARCH_ENGINE_ID": "custom-engine-id",
        "ROUTER_S1_ENDPOINT": "projects/test/locations/us/endpoints/custom-s1",
        "ROUTER_S1_BASE_URL": "https://dedicated.endpoint.test",
        "ORDER_LOOKUP_MCP_URL": "https://order-lookup.test",
        "TICKET_DB_MCP_URL": "https://ticket-db.test",
        "HUMAN_QUEUE_URL": "https://human-queue.test",
        "FAQ_RESOLVER_URL": "https://faq-resolver.test",
        "FAQ_RESOLVER_AUDIENCE": "https://faq-resolver-aud.test",
        "ALLOWED_CALLERS": "sa-triage@test.iam.gserviceaccount.com, sa-admin@test.iam.gserviceaccount.com",
        "REQUIRE_REAL_DEPS": "0",
    }
    cfg = Settings.from_env(env=custom_env)
    assert cfg.project == "test-project-123"
    assert cfg.location == "europe-west1"
    assert cfg.agent_search_location == "us-central1"
    assert cfg.agent_search_engine_id == "custom-engine-id"
    assert cfg.router_s1_endpoint == "projects/test/locations/us/endpoints/custom-s1"
    assert cfg.router_s1_base_url == "https://dedicated.endpoint.test"
    assert cfg.order_lookup_mcp_url == "https://order-lookup.test"
    assert cfg.ticket_db_mcp_url == "https://ticket-db.test"
    assert cfg.human_queue_url == "https://human-queue.test"
    assert cfg.faq_resolver_url == "https://faq-resolver.test"
    assert cfg.faq_resolver_audience == "https://faq-resolver-aud.test"
    assert len(cfg.allowed_callers) == 2
    assert "sa-triage@test.iam.gserviceaccount.com" in cfg.allowed_callers
    assert "sa-admin@test.iam.gserviceaccount.com" in cfg.allowed_callers
    assert cfg.require_real_deps is False


def test_settings_validate_for_faq_missing_raises():
    """Verify validation fails fast when REQUIRE_REAL_DEPS=1 and required vars are missing."""
    empty_env = {"REQUIRE_REAL_DEPS": "1"}
    cfg = Settings.from_env(env=empty_env)
    with pytest.raises(ValueError) as excinfo:
        cfg.validate_for_faq()
    err = str(excinfo.value)
    assert "ORDER_LOOKUP_MCP_URL" in err
    assert "ALLOWED_CALLERS" in err


def test_settings_validate_for_faq_passes():
    """Verify validation succeeds when all required vars for FaqResolver are present."""
    complete_faq_env = {
        "REQUIRE_REAL_DEPS": "1",
        "GOOGLE_CLOUD_PROJECT": "susmith-fde-ticket-router",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "AGENT_SEARCH_ENGINE_ID": "support-kb-search",
        "ROUTER_S1_ENDPOINT": "projects/p/locations/l/endpoints/e",
        "ORDER_LOOKUP_MCP_URL": "https://order-lookup.run.app",
        "ALLOWED_CALLERS": "sa-triage@susmith-fde-ticket-router.iam.gserviceaccount.com",
    }
    cfg = Settings.from_env(env=complete_faq_env)
    cfg.validate_for_faq()
    cfg.validate("faq")


def test_settings_validate_for_triage_missing_raises():
    """Verify validation fails fast when REQUIRE_REAL_DEPS=1 and triage vars are missing."""
    empty_env = {"REQUIRE_REAL_DEPS": "1"}
    cfg = Settings.from_env(env=empty_env)
    with pytest.raises(ValueError) as excinfo:
        cfg.validate_for_triage()
    err = str(excinfo.value)
    assert "TICKET_DB_MCP_URL" in err
    assert "FAQ_RESOLVER_URL" in err


def test_settings_validate_for_triage_passes():
    """Verify validation succeeds when all required vars for TriageRouter are present."""
    complete_triage_env = {
        "REQUIRE_REAL_DEPS": "1",
        "GOOGLE_CLOUD_PROJECT": "susmith-fde-ticket-router",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "ROUTER_S1_ENDPOINT": "projects/p/locations/l/endpoints/e",
        "TICKET_DB_MCP_URL": "https://ticket-db.run.app",
        "FAQ_RESOLVER_URL": "https://faq-resolver.run.app",
    }
    cfg = Settings.from_env(env=complete_triage_env)
    cfg.validate_for_triage()
    cfg.validate("triage")


def test_settings_require_real_deps_disabled_bypasses():
    """Verify that setting REQUIRE_REAL_DEPS=0 bypasses validation checks."""
    cfg = Settings.from_env(env={"REQUIRE_REAL_DEPS": "0"})
    cfg.validate_for_faq()
    cfg.validate_for_triage()
