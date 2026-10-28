"""agent_tuning.py — Dynamic loader and accessors for agent tuning knobs and prompts.

# REQ-EVAL-008: Controlled Hill-Climbing Protocol & Config Registry
"""

from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TUNING_FILE = REPO_ROOT / "src" / "ticket_router" / "config" / "agent_tuning.yaml"
PROMPTS_DIR = REPO_ROOT / "prompts"


@dataclass(frozen=True)
class AgentTuningConfig:
    """Strongly-typed container for all agent tuning knobs."""

    # Router thresholds
    tau_esc: float = 0.35
    tau_low: float = 0.15
    tau_intent: float = 0.70

    # Prompt versions
    tier2_router_version: str = "v1"
    faq_answer_version: str = "v1"

    # Retrieval knobs
    search_config: str = "intent_soft_noisy_0.2"
    top_k: int = 5
    rerank_enabled: bool = False
    citation_strictness: float = 0.70


_ACTIVE_CONFIG: Optional[AgentTuningConfig] = None


def load_yaml_simple(path: Path) -> Dict[str, Any]:
    """Load YAML using PyYAML if available, or lightweight parser fallback."""
    if not path.is_file():
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except ImportError:
        pass

    # Lightweight fallback for flat/nested basic YAML
    result: Dict[str, Any] = {}
    current_section: Optional[str] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            if line.startswith(" ") and current_section:
                parts = line_str.split(":", 1)
                if len(parts) == 2:
                    k, v = parts[0].strip(), parts[1].strip().split("#")[0].strip()
                    val: Any = v
                    if v.lower() == "true":
                        val = True
                    elif v.lower() == "false":
                        val = False
                    else:
                        try:
                            val = int(v) if "." not in v else float(v)
                        except ValueError:
                            val = v.strip('"').strip("'")
                    result.setdefault(current_section, {})[k] = val
            elif ":" in line_str:
                parts = line_str.split(":", 1)
                k = parts[0].strip()
                v = parts[1].strip()
                if not v:
                    current_section = k
                else:
                    current_section = None
                    result[k] = v.strip('"').strip("'")
    return result


_CONTEXT_TIER2_VERSION: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tier2_version", default=None
)


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge_dict(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_tuning_config(
    path: Optional[Path] = None,
    overlay: Optional[Dict[str, Any]] = None,
) -> AgentTuningConfig:
    """Load agent tuning config from file or environment override, optionally merged with overlay."""
    target_path = path or Path(os.environ.get("AGENT_TUNING_CONFIG_PATH", str(DEFAULT_TUNING_FILE)))
    data = load_yaml_simple(target_path) if target_path.is_file() else {}
    if overlay:
        data = _deep_merge_dict(data, overlay)

    router_data = data.get("router", {})
    prompts_data = data.get("prompts", {})
    retr_data = data.get("retrieval", {})

    return AgentTuningConfig(
        tau_esc=float(router_data.get("tau_esc", 0.35)),
        tau_low=float(router_data.get("tau_low", 0.15)),
        tau_intent=float(router_data.get("tau_intent", 0.70)),
        tier2_router_version=str(prompts_data.get("tier2_router_version", "v1")),
        faq_answer_version=str(prompts_data.get("faq_answer_version", "v1")),
        search_config=str(retr_data.get("search_config", "intent_soft_noisy_0.2")),
        top_k=int(retr_data.get("top_k", 5)),
        rerank_enabled=bool(retr_data.get("rerank_enabled", False)),
        citation_strictness=float(retr_data.get("citation_strictness", 0.70)),
    )


def get_tuning_config(
    reload: bool = False,
    overlay: Optional[Dict[str, Any]] = None,
) -> AgentTuningConfig:
    """Retrieve global cached tuning config, optionally merged with overlay."""
    global _ACTIVE_CONFIG
    if overlay is not None:
        return load_tuning_config(overlay=overlay)
    if _ACTIVE_CONFIG is None or reload:
        _ACTIVE_CONFIG = load_tuning_config()
    return _ACTIVE_CONFIG


def get_tier2_prompt(message: str, version: Optional[str] = None) -> str:
    """Render Tier 2 Flash router prompt using configured version."""
    cfg = get_tuning_config()
    ver = version or _CONTEXT_TIER2_VERSION.get() or cfg.tier2_router_version
    template_file = PROMPTS_DIR / f"tier2_router_{ver}.yaml"

    if template_file.is_file():
        data = load_yaml_simple(template_file)
        template = data.get("template")
        if template:
            try:
                return template.format(text=message, message=message)
            except Exception:
                pass

    # Standard default prompt matching HEAD byte-for-byte
    return (
        f"Evaluate the following support ticket:\n{message}\n"
        "Return JSON with: escalate (bool), escalate_prob (float), "
        "sentiment (str: neutral/angry), urgency (str: normal/immediate), "
        "intent (str: billing/shipping/refund/technical/other), intent_prob (float)."
    )


def get_faq_answer_prompt(
    query: str,
    docs_context: Any = "",
    order_context: Any = "{}",
    feedback: Optional[str] = None,
    version: Optional[str] = None,
    context_str: Optional[str] = None,
) -> str:
    """Render FaqResolver answer prompt using configured version."""
    import json

    # Format docs context to JSON string if passed as sequence
    if isinstance(docs_context, (list, tuple)):
        docs_str = json.dumps(list(docs_context), indent=2)
    elif isinstance(docs_context, str) and docs_context:
        docs_str = docs_context
    elif context_str:
        docs_str = context_str
    else:
        docs_str = "[]"

    # Format order context to JSON string if passed as dict
    if isinstance(order_context, dict):
        order_str = json.dumps(order_context or {}, indent=2)
    elif isinstance(order_context, str) and order_context:
        order_str = order_context
    else:
        order_str = "{}"

    feedback_block = f"Correction Feedback:\n{feedback}\n\n" if feedback else ""

    cfg = get_tuning_config()
    ver = version or cfg.faq_answer_version
    template_file = PROMPTS_DIR / f"faq_answer_{ver}.yaml"

    if template_file.is_file():
        data = load_yaml_simple(template_file)
        template = data.get("template")
        if template:
            try:
                return template.format(
                    query=query,
                    docs_context=docs_str,
                    order_context=order_str,
                    feedback_block=feedback_block,
                )
            except Exception:
                pass

    # Standard fallback prompt matching HEAD byte-for-byte
    prompt = (
        f"Customer Inquiry: {query}\n\n"
        f"Retrieved Documents:\n{docs_str}\n\n"
        f"Order Context:\n{order_str}\n\n"
    )
    if feedback:
        prompt += f"Correction Feedback:\n{feedback}\n\n"

    prompt += (
        "Provide a helpful answer grounded strictly in the provided documents.\n"
        "Return valid JSON strictly matching:\n"
        '{\n  "answer": "your answer here",\n  "citations": [{"doc_id": "document_id", "quote": "verbatim text quotation"}]\n}\n'
        "Every answer MUST include at least one citation with a verbatim quotation from the retrieved documents."
    )
    return prompt
