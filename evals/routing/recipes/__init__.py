"""Recipe definitions and loader for routing evaluations."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional

import yaml

_RECIPES_DIR = pathlib.Path(__file__).resolve().parent
_DEFAULT_RECIPE_NAME = "routing_v1"


def get_recipe_path(name: str = _DEFAULT_RECIPE_NAME) -> pathlib.Path:
    """Return filesystem path to a named recipe YAML."""
    path = _RECIPES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Recipe '{name}' not found at: {path}")
    return path


def load_recipe(
    name: str = _DEFAULT_RECIPE_NAME,
    recipe_path: Optional[pathlib.Path | str] = None,
) -> Dict[str, Any]:
    """Load and validate a declarative dataset recipe YAML."""
    path = pathlib.Path(recipe_path) if recipe_path else get_recipe_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Recipe file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}

    # Validate essential top-level keys
    required_keys = ["dataset_version", "seed", "quotas", "sources"]
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ValueError(f"Recipe '{path}' missing required keys: {missing}")

    return data
