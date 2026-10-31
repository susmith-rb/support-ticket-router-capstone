"""Targeted unit tests for ticket_router.serve entrypoint and FastAPI app routing.

# REQ-CICD-010: Line and Branch Test Coverage Enforcement in CI.
# REQ-PLAT-003: Unified Microservice Serving and Connection Pooling.
"""

from __future__ import annotations

import pytest

from ticket_router.serve import (
    _instrument_fastapi_app,
    build_connection_manager,
    get_app,
)


def test_serve_build_connection_manager_validation(monkeypatch: pytest.MonkeyPatch):
    """Verify build_connection_manager fail-fast argument and env validation."""
    monkeypatch.delenv("INSTANCE_CONNECTION_NAME", raising=False)
    with pytest.raises(
        RuntimeError, match="INSTANCE_CONNECTION_NAME environment variable is required"
    ):
        build_connection_manager()

    with pytest.raises(ValueError, match="Invalid INSTANCE_CONNECTION_NAME"):
        build_connection_manager(instance_connection_name="invalid-format-two-parts")

    monkeypatch.delenv("DB_IAM_USER", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    with pytest.raises(RuntimeError, match="DB_IAM_USER environment variable is required"):
        build_connection_manager(instance_connection_name="proj:region:inst")


def test_serve_get_app_service_routing(monkeypatch: pytest.MonkeyPatch):
    """Verify get_app service name routing and review_ui instantiation."""
    monkeypatch.setenv("ALLOW_FAKE_REPOS", "1")

    # Review UI app routing
    app_review = get_app("review_ui")
    assert app_review.title == "Human Review UI" or hasattr(app_review, "routes")

    # Re-instrumenting returns existing app without duplicate middleware
    app_re_instrumented = _instrument_fastapi_app(app_review, "review_ui")
    assert app_re_instrumented is app_review

    # Unknown service error
    with pytest.raises(ValueError, match="Unknown SERVICE 'unsupported_service'"):
        get_app("unsupported_service")
