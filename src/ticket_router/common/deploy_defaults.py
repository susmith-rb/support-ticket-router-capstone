"""Runtime deployment defaults reader.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)

Reads deploy-time defaults from src/ticket_router/config/runtime_defaults.yaml (single source of truth).
Ensures application code across src/ contains zero hardcoded environment literals.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_DEFAULTS_CACHE: Optional[Dict[str, Any]] = None


def _find_defaults_file() -> Optional[Path]:
    if "DEPLOY_DEFAULTS_PATH" in os.environ:
        p = Path(os.environ["DEPLOY_DEFAULTS_PATH"])
        if p.is_file():
            return p
    # Packaged next to models.yaml so it ships in every runtime (Docker COPY src/,
    # Agent Engine /code/src); deploy/ is NOT in the container image.
    candidate = Path(__file__).resolve().parents[1] / "config" / "runtime_defaults.yaml"
    if candidate.is_file():
        return candidate
    return None


def load_deploy_defaults() -> Dict[str, Any]:
    """Load and cache dictionary of deploy-time environment defaults."""
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is not None:
        return _DEFAULTS_CACHE
    path = _find_defaults_file()
    if path is not None and path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    _DEFAULTS_CACHE = data
                    return _DEFAULTS_CACHE
        except Exception as exc:  # malformed file must be visible, not silently empty
            raise RuntimeError(f"Invalid deploy defaults file {path}: {exc}") from exc
    _DEFAULTS_CACHE = {}
    return _DEFAULTS_CACHE


def get_deploy_default(key_path: str, default: str = "") -> str:
    """Fetch nested dot-delimited key from deploy defaults (e.g. 'endpoints.router_s1_endpoint')."""
    data = load_deploy_defaults()
    parts = key_path.split(".")
    curr: Any = data
    for part in parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return default
    return str(curr) if curr is not None else default
