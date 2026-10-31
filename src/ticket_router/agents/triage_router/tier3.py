"""Tier 3 multihop reasoning fallback using Gemini Pro (REQ-AGENT-002, REQ-AGENT-013).

Provides a narrow, budgeted fallback for TriagePolicy when Tier 2 Flash returns
low confidence or an explicit needs_multihop/ambiguous signal for non-escalation intents.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ticket_router.common.hedging import hedged_call
from ticket_router.common.resilience import TIER3_PRO_DEADLINE_S
from ticket_router.config.agent_tuning import (
    get_tier2_prompt,
    get_tier2_system_instruction,
)
from ticket_router.config.models import (
    get as get_model_cfg,
)
from ticket_router.config.models import (
    model_id,
    model_location,
    model_thinking_level,
)
from ticket_router.models.router_s1_client import RouterPrediction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier3Config:
    """Configuration knobs for Tier 3 Gemini Pro fallback."""

    enabled: bool = True
    confidence_threshold: float = 0.70
    deadline_s: float = TIER3_PRO_DEADLINE_S

    @classmethod
    def from_config(cls) -> Tier3Config:
        """Resolve Tier 3 configuration from models.yaml and environment overrides."""
        try:
            cfg = get_model_cfg("tier3_multihop")
            base_enabled = bool(cfg.get("enabled", True))
            base_conf = float(cfg.get("confidence_threshold", 0.70))
            base_deadline = float(cfg.get("deadline_s", TIER3_PRO_DEADLINE_S))
        except Exception:
            base_enabled = True
            base_conf = 0.70
            base_deadline = TIER3_PRO_DEADLINE_S

        raw_enabled = os.environ.get("TIER3_ENABLED")
        if raw_enabled is not None:
            enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on")
        else:
            enabled = base_enabled

        raw_conf = os.environ.get("TIER3_CONFIDENCE_THRESHOLD")
        if raw_conf is not None:
            try:
                confidence_threshold = float(raw_conf.strip())
            except ValueError:
                confidence_threshold = base_conf
        else:
            confidence_threshold = base_conf

        raw_deadline = os.environ.get("TIER3_DEADLINE_S")
        if raw_deadline is not None:
            try:
                deadline_s = float(raw_deadline.strip())
            except ValueError:
                deadline_s = base_deadline
        else:
            deadline_s = base_deadline

        return cls(
            enabled=enabled,
            confidence_threshold=confidence_threshold,
            deadline_s=deadline_s,
        )


def is_tier3_trigger(
    t2_pred: RouterPrediction,
    confidence_threshold: float = 0.70,
) -> Tuple[bool, bool]:
    """Evaluate whether Tier 2 prediction triggers a Tier 3 fallback call.

    Returns:
        (should_trigger, is_low_confidence)
    """
    # Escalation paths are add-only and MUST NOT trigger Tier 3
    if (
        t2_pred.escalate
        or t2_pred.sentiment == "angry"
        or t2_pred.urgency == "immediate"
    ):
        return False, False

    # Low confidence on intent or escalation certainty
    is_low_confidence = (
        t2_pred.intent_prob < confidence_threshold
        or (1.0 - t2_pred.escalate_prob) < confidence_threshold
    )

    # Explicit signal from model reasoning / metadata
    raw_meta = t2_pred.raw if isinstance(t2_pred.raw, dict) else {}
    has_multihop_signal = (
        bool(raw_meta.get("needs_multihop"))
        or bool(raw_meta.get("ambiguous"))
        or str(raw_meta.get("needs_multihop", "")).strip().lower() in ("true", "yes", "1")
        or str(raw_meta.get("ambiguous", "")).strip().lower() in ("true", "yes", "1")
        or t2_pred.intent in ("needs_multihop", "ambiguous", "multihop")
        or str(raw_meta.get("signal", "")).strip().lower() in ("needs_multihop", "ambiguous", "multihop")
        or str(raw_meta.get("reason", "")).strip().lower() in ("needs_multihop", "ambiguous", "multihop")
    )

    should_trigger = is_low_confidence or has_multihop_signal
    return should_trigger, is_low_confidence


def invoke_tier3_gemini(
    text: str,
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    deadline_s: Optional[float] = None,
) -> RouterPrediction:
    """Execute Tier 3 fallback inference using Gemini Pro (REQ-AGENT-002, REQ-AGENT-013).

    Reuses client initialization and prompt plumbing from Tier 2, passing thinking level
    and enforcing a single budgeted call with hard deadline.
    """
    model_role = "tier3_multihop"
    resolved_model = target_model or model_id(model_role)
    resolved_location = location or model_location(model_role) or os.environ.get("GOOGLE_CLOUD_LOCATION")
    effective_project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    effective_deadline = (
        deadline_s if deadline_s is not None else Tier3Config.from_config().deadline_s
    )

    try:
        from google import genai  # type: ignore[import-untyped]

        client_kwargs: Dict[str, Any] = {}
        if effective_project:
            client_kwargs["project"] = effective_project
            client_kwargs["vertexai"] = True
        if resolved_location:
            client_kwargs["location"] = resolved_location
        client = genai.Client(**client_kwargs)

        prompt = get_tier2_prompt(text)
        sys_inst = get_tier2_system_instruction()
        thinking_lvl = model_thinking_level(model_role)

        call_kwargs: Dict[str, Any] = {
            "model": resolved_model,
            "contents": prompt,
        }
        if sys_inst or thinking_lvl:
            gen_cfg: Any = None
            try:
                from google.genai import types as genai_types  # type: ignore[import-untyped]

                if hasattr(genai_types, "GenerateContentConfig"):
                    cfg_kwargs: Dict[str, Any] = {}
                    if sys_inst:
                        cfg_kwargs["system_instruction"] = sys_inst
                    if thinking_lvl:
                        if hasattr(genai_types, "ThinkingConfig"):
                            cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                                thinking_level=thinking_lvl
                            )
                        else:
                            cfg_kwargs["thinking_config"] = {"thinking_level": thinking_lvl}
                    gen_cfg = genai_types.GenerateContentConfig(**cfg_kwargs)
                else:
                    dict_cfg: Dict[str, Any] = {}
                    if sys_inst:
                        dict_cfg["system_instruction"] = sys_inst
                    if thinking_lvl:
                        dict_cfg["thinking_config"] = {"thinking_level": thinking_lvl}
                    gen_cfg = dict_cfg
            except Exception:
                dict_cfg = {}
                if sys_inst:
                    dict_cfg["system_instruction"] = sys_inst
                if thinking_lvl:
                    dict_cfg["thinking_config"] = {"thinking_level": thinking_lvl}
                gen_cfg = dict_cfg
            call_kwargs["config"] = gen_cfg

        # Single budgeted call: at most 1 attempt with hard deadline
        resp = hedged_call(
            lambda: client.models.generate_content(**call_kwargs),
            hedge_after_s=effective_deadline,
            deadline_s=effective_deadline,
            max_attempts=1,
            span_name="tier3.generate_content",
        )

        usage_data: Optional[Dict[str, int]] = None
        usage_meta = getattr(resp, "usage_metadata", None)
        if usage_meta is not None:
            pt = getattr(usage_meta, "prompt_token_count", None)
            ct = getattr(usage_meta, "candidates_token_count", None)
            tt = getattr(usage_meta, "total_token_count", None)
            if pt is not None or ct is not None or tt is not None:
                p_int = int(pt) if pt is not None else 0
                c_int = int(ct) if ct is not None else 0
                t_int = int(tt) if tt is not None else (p_int + c_int)
                usage_data = {
                    "prompt_tokens": p_int,
                    "output_tokens": c_int,
                    "total_tokens": t_int,
                }
        extra_raw: Dict[str, Any] = {
            "model_id": resolved_model,
            "usage": usage_data,
        }

        raw_text = ""
        try:
            raw_text = str(resp.text)
        except AttributeError:
            raw_text = ""

        clean_text = raw_text.strip()
        if clean_text.startswith("```"):
            lines = clean_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_text = "\n".join(lines).strip()

        try:
            parsed = json.loads(clean_text)
            if isinstance(parsed, dict) and (
                "escalate" in parsed or "intent" in parsed or "sentiment" in parsed
            ):
                esc_raw = parsed.get("escalate", False)
                if isinstance(esc_raw, bool):
                    is_esc = esc_raw
                elif isinstance(esc_raw, str):
                    is_esc = esc_raw.strip().lower() in ("true", "yes", "1", "escalate")
                else:
                    is_esc = bool(esc_raw)

                sentiment = str(parsed.get("sentiment") or ("angry" if is_esc else "neutral")).strip().lower()
                urgency = str(parsed.get("urgency") or "normal").strip().lower()
                if sentiment == "angry" or urgency == "immediate":
                    is_esc = True

                oos_raw = parsed.get("out_of_scope", False)
                if isinstance(oos_raw, bool):
                    is_oos = oos_raw
                elif isinstance(oos_raw, str):
                    is_oos = oos_raw.strip().lower() in ("true", "yes", "1")
                else:
                    is_oos = bool(oos_raw)

                if is_esc:
                    is_oos = False

                if is_oos:
                    intent = "other"
                else:
                    intent = str(parsed.get("intent") or ("other" if is_esc else "faq")).strip()

                esc_prob = float(parsed.get("escalate_prob", 0.85 if is_esc else 0.10))
                intent_prob = float(parsed.get("intent_prob", 0.80))
                merged_raw = {**parsed, **extra_raw}
                return RouterPrediction(
                    escalate=is_esc,
                    escalate_prob=esc_prob,
                    sentiment=sentiment,
                    sentiment_prob=0.85,
                    urgency=urgency,
                    urgency_prob=0.85,
                    intent=intent,
                    intent_prob=intent_prob,
                    latency_ms=120.0,
                    raw=merged_raw,
                    out_of_scope=is_oos,
                )
        except Exception:
            pass

        is_angry = "angry" in raw_text.lower()
        is_urgent = "immediate" in raw_text.lower()
        is_esc = "escalate" in raw_text.lower() or is_angry or is_urgent
        fallback_raw = {"raw_text": raw_text, **extra_raw}
        return RouterPrediction(
            escalate=is_esc,
            escalate_prob=0.85 if is_esc else 0.10,
            sentiment="angry" if is_angry else "neutral",
            sentiment_prob=0.85,
            urgency="immediate" if is_urgent else "normal",
            urgency_prob=0.85,
            intent="other" if is_esc else "faq",
            intent_prob=0.80,
            latency_ms=120.0,
            raw=fallback_raw,
            out_of_scope=False,
        )
    except Exception as exc:
        raise RuntimeError(f"Tier 3 model call failed ({resolved_model}): {exc}") from exc


@dataclass(frozen=True)
class Tier3GeminiInvoker:
    """Picklable top-level callable invoking Tier 3 Gemini Pro with configured project/location/budget."""

    model_role: str = "tier3_multihop"
    project: Optional[str] = None
    location: Optional[str] = None
    deadline_s: Optional[float] = None

    def __call__(self, text: str) -> RouterPrediction:
        resolved_model = model_id(self.model_role)
        resolved_location = model_location(self.model_role) or self.location
        proj = self.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
        return invoke_tier3_gemini(
            text,
            target_model=resolved_model,
            project=proj,
            location=resolved_location,
            deadline_s=self.deadline_s,
        )


def default_tier3_invoker(text: str) -> RouterPrediction:
    """Default module-scope Tier 3 invoker resolving model dynamically."""
    return Tier3GeminiInvoker()(text)


def create_tier3_invoker(
    model_role: str = "tier3_multihop",
    custom_caller: Optional[Callable[[str], RouterPrediction]] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    deadline_s: Optional[float] = None,
) -> Callable[[str], RouterPrediction]:
    """Build a Tier 3 fallback invoker using Gemini Pro (REQ-AGENT-002, REQ-AGENT-013)."""
    if custom_caller is not None:
        return custom_caller
    # Validate model_role exists in registry at creation time
    _ = model_id(model_role)
    if (
        model_role == "tier3_multihop"
        and project is None
        and location is None
        and deadline_s is None
    ):
        return default_tier3_invoker
    return Tier3GeminiInvoker(
        model_role=model_role,
        project=project,
        location=location,
        deadline_s=deadline_s,
    )
