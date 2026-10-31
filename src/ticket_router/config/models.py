"""Central model configuration loader.

REQ-BILL-MODELS-001: Centralized Model Configuration Registry.
Provides get(role) and model_id(role) with strict precedence:
1. Environment variable MODEL_<ROLE_UPPER>
2. envs[ENV] environment override block from models.yaml
3. Default role mapping from models.yaml

Includes a zero-dependency YAML fallback parser so environments without PyYAML
(e.g. lightweight CI/CD containers) can load model configurations safely.
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any, Dict, Optional

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent
_CONFIG_FILE = _CONFIG_DIR / "models.yaml"

_REGISTRY_CACHE: Optional[Dict[str, Any]] = None


def _parse_yaml_fallback(text: str) -> Dict[str, Any]:
    """Zero-dependency parser for models.yaml and nested overrides.

    Used when PyYAML is not installed (e.g. cloud-sdk:slim containers).
    """
    root: Dict[str, Any] = {}
    stack: list[tuple[int, Dict[str, Any]]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))

        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()

        parent_dict = stack[-1][1]

        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()

            if not val or val.startswith("#"):
                new_dict: Dict[str, Any] = {}
                parent_dict[key] = new_dict
                stack.append((indent, new_dict))
            elif val == "{}":
                parent_dict[key] = {}
            else:
                parent_dict[key] = _clean_val(val)

    if "models" not in root:
        root["models"] = {}
    if "envs" not in root:
        root["envs"] = {}
    if "serving" not in root:
        root["serving"] = {}
    return root


def _clean_val(val_str: str) -> Any:
    val = val_str.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val == "{}":
        return {}
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_registry(config_path: Optional[pathlib.Path] = None, reload: bool = False) -> Dict[str, Any]:
    """Load models registry from YAML file or cache."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None and not reload and config_path is None:
        return _REGISTRY_CACHE

    target = config_path or _CONFIG_FILE
    if not target.exists():
        raise FileNotFoundError(f"Model configuration file not found at: {target}")

    text = target.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = _parse_yaml_fallback(text)

    if config_path is None and not reload:
        _REGISTRY_CACHE = data
    return data


def get(role: str, env: Optional[str] = None, config_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Retrieve full configuration dictionary for a logical model role.

    Precedence:
    1. Env var: MODEL_<ROLE_UPPER>
    2. envs[ENV] override block
    3. Default entry in models[role]

    Raises:
        KeyError: If role is not defined in models registry.
    """
    data = load_registry(config_path=config_path)
    models = data.get("models", {})
    envs = data.get("envs", {})

    target_env = env or os.environ.get("ENV") or os.environ.get("ENVIRONMENT")
    if target_env:
        target_env = target_env.lower().strip()

    if role not in models and (not target_env or target_env not in envs or role not in envs[target_env]):
        raise KeyError(
            f"Unknown model role: '{role}'. Available roles: {sorted(models.keys())}"
        )

    # 1. Base config
    base = dict(models.get(role, {}))

    # 2. Env override
    if target_env and target_env in envs:
        env_override = envs[target_env].get(role)
        if isinstance(env_override, dict):
            base.update(env_override)
        elif isinstance(env_override, str):
            base["id"] = env_override

    # 3. Environment variable override
    env_var_key = f"MODEL_{re.sub(r'[^A-Za-z0-9]', '_', role).upper()}"
    override_val = os.environ.get(env_var_key)
    if override_val:
        base["id"] = override_val.strip()

    if "id" not in base:
        raise KeyError(f"Model role '{role}' does not specify an 'id'")

    return base


def model_id(role: str, env: Optional[str] = None, config_path: Optional[pathlib.Path] = None) -> str:
    """Retrieve model ID string for a logical model role."""
    return str(get(role, env=env, config_path=config_path)["id"])


def model_location(
    role: str,
    default: Optional[str] = None,
    env: Optional[str] = None,
    config_path: Optional[pathlib.Path] = None,
) -> Optional[str]:
    """Retrieve model serving location string for a logical model role if present else default.

    # REQ-AGENT-013: Centralized Model Configuration Resolution.
    """
    try:
        cfg = get(role, env=env, config_path=config_path)
    except KeyError:
        return default
    loc = cfg.get("location")
    return str(loc) if loc is not None else default


def model_thinking_level(
    role: str,
    default: Optional[str] = None,
    env: Optional[str] = None,
    config_path: Optional[pathlib.Path] = None,
) -> Optional[str]:
    """Retrieve thinking level string (e.g. 'LOW') for a logical model role if present else default."""
    try:
        cfg = get(role, env=env, config_path=config_path)
    except KeyError:
        return default
    lvl = cfg.get("thinking_level")
    if lvl is None:
        lvl = cfg.get("thinking")
    return str(lvl) if lvl is not None else default


def list_roles(config_path: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """Return dictionary of role -> resolved model ID."""
    data = load_registry(config_path=config_path)
    return {role: model_id(role, config_path=config_path) for role in data.get("models", {})}


def get_serving_config(service: str = "router_s1", config_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Retrieve serving configuration section for a given service."""
    data = load_registry(config_path=config_path)
    serving = data.get("serving", {})
    return dict(serving.get(service, {}))


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Model Configuration CLI")
    parser.add_argument("role", nargs="?", default=None, help="Logical role to look up")
    parser.add_argument("--json", action="store_true", help="Output full role config as JSON")
    parser.add_argument("--list", action="store_true", help="List all configured roles")
    parser.add_argument("--env", default=None, help="Override environment (e.g. dev, prod)")

    args = parser.parse_args(argv)

    if args.list or not args.role:
        roles = list_roles()
        if args.json:
            print(json.dumps(roles, indent=2))
        else:
            for r, mid in sorted(roles.items()):
                print(f"{r}: {mid}")
        return 0

    try:
        cfg = get(args.role, env=args.env)
        if args.json:
            print(json.dumps(cfg, indent=2))
        else:
            print(cfg["id"])
        return 0
    except KeyError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
