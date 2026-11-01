"""Gemini Flash Turn Interpreter with structured output.

# REQ-TURN-001: Natural Language Interpretation via Gemini Flash Structured Output
# REQ-TURN-003: Pure Tiered Routing Order: Laya Fast Path vs Flash Interpreter
# REQ-TURN-004: Deterministic Enforcement of Turn Acts and Security Refusals
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field

from ticket_router.agents.triage_router.conversation_state import ConversationState
from ticket_router.common.hedging import hedged_call
from ticket_router.common.resilience import (
    TIER2_DEADLINE_S,
    TIER2_HEDGE_AFTER_S,
    ServiceTimeoutError,
)
from ticket_router.config.models import model_id, model_location, model_thinking_level

logger = logging.getLogger(__name__)

AllowedTurnAct = Literal[
    "new_request",
    "followup_to_active_ticket",
    "confirmation_yes",
    "confirmation_no",
    "conditional_resolution",
    "resolution_report",
    "other",
]

AllowedIntent = Literal[
    "order_status",
    "shipping_policy",
    "returns_refunds",
    "account_payment",
    "conversation_history_access",
    "other",
]

AllowedSentiment = Literal["angry", "frustrated", "neutral", "positive"]

AllowedUrgency = Literal["can wait days", "within a day", "within hours", "immediate"]


class IntentSpan(BaseModel):
    """Specific intent span identified within customer text."""
    intent: str = Field(description="Intent name matching retail taxonomy")
    text_span: str = Field(description="Exact or approximate substring corresponding to intent")


class SecurityFlags(BaseModel):
    """Discrete security signals extracted from customer message."""
    cross_customer_data_request: bool = Field(
        default=False,
        description="Whether message requests another customer's orders, accounts, or conversation logs",
    )
    authorization_bypass_attempt: bool = Field(
        default=False,
        description="Whether message attempts to bypass auth checks or claims admin/root privileges",
    )
    conversation_history_request: bool = Field(
        default=False,
        description="Whether message requests access to own conversation history, chat logs, or interaction summaries",
    )


class TurnInterpretation(BaseModel):
    """Structured semantic interpretation produced by Gemini Flash for a conversation turn."""
    turn_act: AllowedTurnAct = Field(description="High-level dialog act for the current turn")
    intents: List[IntentSpan] = Field(
        default_factory=list,
        description="List of all detected retail intents with associated text spans",
    )
    primary_intent: str = Field(description="Primary retail intent")
    escalate: bool = Field(description="Whether the turn requires human escalation")
    escalation_reason: Optional[str] = Field(default=None, description="Reason code if escalate is true")
    sentiment: AllowedSentiment = Field(default="neutral", description="Customer emotional sentiment")
    urgency: AllowedUrgency = Field(default="within a day", description="Customer urgency assessment")
    out_of_scope: bool = Field(default=False, description="Whether query is completely out of retail scope")
    security: SecurityFlags = Field(default_factory=SecurityFlags, description="Extracted security flags")
    referenced_ticket: Optional[str] = Field(default=None, description="TICK-... reference token if referenced")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Model confidence estimate")
    latency_ms: float = Field(default=0.0, description="Inference latency in milliseconds")
    details: Dict[str, Any] = Field(default_factory=dict, description="Optional extra metadata")


def _resolve_turn_interpreter_prompt_file() -> Path:
    """Find prompts/turn_interpreter_v1.yaml across repo and runtime environments."""
    repo_root = Path(__file__).resolve().parents[4]
    candidates = [
        repo_root / "prompts" / "turn_interpreter_v1.yaml",
        Path("/app/prompts/turn_interpreter_v1.yaml"),
        Path("/code/prompts/turn_interpreter_v1.yaml"),
        Path.cwd() / "prompts" / "turn_interpreter_v1.yaml",
    ]
    env_dir = os.environ.get("AGENT_PROMPTS_DIR")
    if env_dir:
        candidates.insert(0, Path(env_dir) / "turn_interpreter_v1.yaml")

    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("Could not find prompts/turn_interpreter_v1.yaml")


def load_turn_interpreter_prompt() -> Tuple[str, str]:
    """Load system instruction and template string from prompts/turn_interpreter_v1.yaml."""
    path = _resolve_turn_interpreter_prompt_file()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sys_inst = str(data.get("system_instruction", "")).strip()
    template = str(data.get("template", "")).strip()
    return sys_inst, template


def format_turn_interpreter_prompt(
    text: str,
    conversation_state: Optional[ConversationState] = None,
    recent_intents: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Format user prompt and system instruction for Gemini Flash Turn Interpreter."""
    sys_inst, template = load_turn_interpreter_prompt()
    state_str = conversation_state.to_prompt_str() if conversation_state else "{}"
    intents_str = json.dumps(recent_intents[-2:], separators=(",", ":")) if recent_intents else "[]"
    contents = template.format(
        conversation_state=state_str,
        recent_intents=intents_str,
        text=text,
    )
    return sys_inst, contents


def clean_json_response(raw_text: str) -> str:
    """Clean markdown json code fences from LLM response text."""
    t = raw_text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def invoke_turn_interpreter(
    text: str,
    conversation_state: Optional[ConversationState] = None,
    recent_intents: Optional[List[str]] = None,
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    client: Optional[Any] = None,
    deadline_s: Optional[float] = None,
    hedge_after_s: Optional[float] = None,
) -> TurnInterpretation:
    """Execute Gemini Flash turn interpreter with structured schema output (REQ-TURN-001)."""
    from ticket_router.models.tier2_gemini import check_gemini_429_hook

    check_gemini_429_hook()
    start_time = time.monotonic()

    resolved_model = target_model or model_id("router_tier2")
    # models.yaml pins the Flash location (global); the agent-wide region (us-central1) returns 404
    # for this model, so the model location wins over the caller-supplied region.
    resolved_location = model_location("router_tier2") or location or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
    effective_project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    effective_deadline = deadline_s if deadline_s is not None else TIER2_DEADLINE_S
    effective_hedge = hedge_after_s if hedge_after_s is not None else TIER2_HEDGE_AFTER_S

    sys_inst, contents = format_turn_interpreter_prompt(
        text=text,
        conversation_state=conversation_state,
        recent_intents=recent_intents,
    )

    try:
        from google import genai
        from google.genai import types as genai_types

        client_instance = client
        if client_instance is None:
            client_kwargs: Dict[str, Any] = {}
            if effective_project:
                client_kwargs["project"] = effective_project
                client_kwargs["vertexai"] = True
            if resolved_location:
                client_kwargs["location"] = resolved_location
            client_instance = genai.Client(**client_kwargs)

        cfg_kwargs: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": TurnInterpretation,
            "temperature": 0.0,
        }
        if sys_inst:
            cfg_kwargs["system_instruction"] = sys_inst
        thinking_lvl = model_thinking_level("router_tier2")
        if thinking_lvl:
            if hasattr(genai_types, "ThinkingConfig"):
                cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_level=thinking_lvl)
            else:
                cfg_kwargs["thinking_config"] = {"thinking_level": thinking_lvl}

        call_config = genai_types.GenerateContentConfig(**cfg_kwargs)

        def _call_model() -> Any:
            return client_instance.models.generate_content(
                model=resolved_model,
                contents=contents,
                config=call_config,
            )

        resp = hedged_call(
            _call_model,
            effective_hedge,
            effective_deadline,
            span_name="turn_interpreter.generate_content",
            span_attributes={"turn_interpreter.model": str(resolved_model)},
        )

        latency_ms = (time.monotonic() - start_time) * 1000.0
        cleaned_json = clean_json_response(resp.text or "")
        interpretation = TurnInterpretation.model_validate_json(cleaned_json)
        interpretation.latency_ms = latency_ms
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            interpretation.details["prompt_tokens"] = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            interpretation.details["candidates_tokens"] = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
        return interpretation

    except ServiceTimeoutError:
        raise
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "event": "turn_interpreter_failed",
                    "model": str(resolved_model),
                    "location": str(resolved_location),
                    "error_type": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )
        raise RuntimeError(f"Turn interpreter call failed ({resolved_model}): {exc}") from exc


class TurnInterpreter:
    """Callable wrapper for TurnInterpreter dependency injection."""

    def __init__(
        self,
        invoker: Optional[Callable[..., TurnInterpretation]] = None,
        target_model: Optional[str] = None,
        project: Optional[str] = None,
        location: Optional[str] = None,
        deadline_s: Optional[float] = None,
        hedge_after_s: Optional[float] = None,
    ) -> None:
        self.invoker = invoker
        self.target_model = target_model
        self.project = project
        self.location = location
        self.deadline_s = deadline_s
        self.hedge_after_s = hedge_after_s

    def __call__(
        self,
        text: str,
        conversation_state: Optional[ConversationState] = None,
        recent_intents: Optional[List[str]] = None,
    ) -> TurnInterpretation:
        if self.invoker is not None:
            return self.invoker(
                text,
                conversation_state=conversation_state,
                recent_intents=recent_intents,
            )
        return invoke_turn_interpreter(
            text,
            conversation_state=conversation_state,
            recent_intents=recent_intents,
            target_model=self.target_model,
            project=self.project,
            location=self.location,
            deadline_s=self.deadline_s,
            hedge_after_s=self.hedge_after_s,
        )


def create_turn_interpreter(
    invoker: Optional[Callable[..., TurnInterpretation]] = None,
    target_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
    deadline_s: Optional[float] = None,
    hedge_after_s: Optional[float] = None,
) -> TurnInterpreter:
    """Factory creating a TurnInterpreter instance."""
    return TurnInterpreter(
        invoker=invoker,
        target_model=target_model,
        project=project,
        location=location,
        deadline_s=deadline_s,
        hedge_after_s=hedge_after_s,
    )
