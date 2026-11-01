"""Centralized Google GenAI client factory.

# REQ-AGENT-013: Centralized Model Configuration Resolution
"""

from __future__ import annotations

from typing import Any, Optional

from ticket_router.config.models import model_location


def get_genai_client(
    role: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> Any:
    """Return a google.genai.Client configured for the given logical model role.

    Precedence for location:
      1. models.yaml location for role (model_location(role))
      2. caller-supplied location
      3. Settings.location (or GOOGLE_CLOUD_LOCATION / default)

    Precedence for project:
      1. caller-supplied project
      2. Settings.project (or GOOGLE_CLOUD_PROJECT / default)
    """
    from google import genai  # type: ignore[import-untyped]

    from ticket_router.common.settings import Settings

    settings = Settings.from_env()

    # Location resolution: models.yaml location strictly takes precedence
    role_loc = model_location(role) if role else None
    resolved_location = role_loc or location or settings.location

    # Project resolution
    resolved_project = project or settings.project

    client_kwargs: dict[str, Any] = {}
    if resolved_project:
        client_kwargs["project"] = resolved_project
        client_kwargs["vertexai"] = True
    if resolved_location:
        client_kwargs["location"] = resolved_location

    return genai.Client(**client_kwargs)
