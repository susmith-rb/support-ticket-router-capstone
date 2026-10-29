"""Taxonomy configuration loader and validation utilities.

# REQ-REVAL-001: Taxonomy single source of truth & label validation.
Provides centralized loading and validation for routing intents, sentiments,
urgency levels, decisions, queues, and expected behaviour policy.
"""

from __future__ import annotations

import functools
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent
_TAXONOMY_FILE = _CONFIG_DIR / "taxonomy.yaml"


@dataclass(frozen=True)
class IntentDefinition:
    """Definition and boundary details for a single intent category."""

    name: str
    definition: str
    positive_examples: Tuple[str, ...]
    boundary_notes: str


@dataclass(frozen=True)
class ExpectedBehaviourPolicy:
    """Policy rules governing escalation triggers and queue assignments."""

    escalate_triggers: Tuple[str, ...]
    queue_rules: Tuple[str, ...]
    decision_rules: Tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    """Frozen taxonomy container representing the single source of truth."""

    intents: Tuple[str, ...]
    intent_definitions: Dict[str, IntentDefinition]
    sentiments: Tuple[str, ...]
    sentiment_criteria: Dict[str, str]
    urgency_levels: Dict[int, str]
    decisions: Tuple[str, ...]
    queues: Tuple[str, ...]
    tiers: Tuple[str, ...]
    expected_behaviour: ExpectedBehaviourPolicy


@functools.lru_cache(maxsize=1)
def load_taxonomy(path: Optional[pathlib.Path | str] = None) -> Taxonomy:
    """Load and cache the centralized routing taxonomy."""
    file_path = pathlib.Path(path) if path else _TAXONOMY_FILE
    if not file_path.exists():
        raise FileNotFoundError(f"Taxonomy configuration file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    raw_intents = raw.get("intents", {})
    intent_defs: Dict[str, IntentDefinition] = {}
    for name, item in raw_intents.items():
        intent_defs[name] = IntentDefinition(
            name=name,
            definition=item.get("definition", ""),
            positive_examples=tuple(item.get("positive_examples", [])),
            boundary_notes=item.get("boundary_notes", ""),
        )

    intents = tuple(raw_intents.keys())

    raw_sentiments = raw.get("sentiments", {})
    sentiments = tuple(raw_sentiments.keys())
    sentiment_criteria = {k: str(v) for k, v in raw_sentiments.items()}

    raw_urgency = raw.get("urgency", {})
    urgency_levels = {int(k): str(v) for k, v in raw_urgency.items()}

    decisions = tuple(raw.get("decisions", []))
    queues = tuple(raw.get("queues", []))
    tiers = tuple(raw.get("tiers", []))

    raw_exp = raw.get("expected_behaviour", {})
    expected_behaviour = ExpectedBehaviourPolicy(
        escalate_triggers=tuple(raw_exp.get("escalate_triggers", [])),
        queue_rules=tuple(raw_exp.get("queue_rules", [])),
        decision_rules=tuple(raw_exp.get("decision_rules", [])),
    )

    return Taxonomy(
        intents=intents,
        intent_definitions=intent_defs,
        sentiments=sentiments,
        sentiment_criteria=sentiment_criteria,
        urgency_levels=urgency_levels,
        decisions=decisions,
        queues=queues,
        tiers=tiers,
        expected_behaviour=expected_behaviour,
    )


def is_valid_intent(intent: str) -> bool:
    """Check if an intent belongs to the canonical taxonomy."""
    tax = load_taxonomy()
    return intent in tax.intents


def validate_expected(d: Dict[str, Any]) -> List[str]:
    """Validate expected behaviour dictionary against taxonomy rules.

    Returns:
        List of error description strings. An empty list indicates a valid expected dict.
    """
    errors: List[str] = []
    if not isinstance(d, dict):
        return [f"Expected value must be a dictionary, got {type(d).__name__}"]

    tax = load_taxonomy()

    # 1. intent validation
    intent = d.get("intent")
    if intent is None:
        errors.append("Field 'intent' is required")
    elif not isinstance(intent, str):
        errors.append(f"Field 'intent' must be a string, got {type(intent).__name__}")
    elif intent not in tax.intents:
        errors.append(
            f"Invalid intent '{intent}'. Must be one of {list(tax.intents)}"
        )

    # 2. acceptable_intents validation
    acc_intents = d.get("acceptable_intents")
    if acc_intents is None:
        errors.append("Field 'acceptable_intents' is required")
    elif not isinstance(acc_intents, (list, tuple)):
        errors.append(
            f"Field 'acceptable_intents' must be a list or tuple, got {type(acc_intents).__name__}"
        )
    else:
        if len(acc_intents) == 0:
            errors.append("Field 'acceptable_intents' cannot be empty")
        for acc in acc_intents:
            if not isinstance(acc, str) or acc not in tax.intents:
                errors.append(
                    f"Invalid intent '{acc}' in 'acceptable_intents'. Must be in {list(tax.intents)}"
                )
        if intent and isinstance(intent, str) and intent not in acc_intents:
            errors.append(
                f"Field 'acceptable_intents' must contain primary intent '{intent}'"
            )

    # 3. decision validation
    decision = d.get("decision")
    if decision is None:
        errors.append("Field 'decision' is required")
    elif not isinstance(decision, str):
        errors.append(
            f"Field 'decision' must be a string, got {type(decision).__name__}"
        )
    elif decision not in tax.decisions:
        errors.append(
            f"Invalid decision '{decision}'. Must be one of {list(tax.decisions)}"
        )

    # 4. queue validation
    queue = d.get("queue")
    if queue is None:
        errors.append("Field 'queue' is required")
    elif not isinstance(queue, str):
        errors.append(f"Field 'queue' must be a string, got {type(queue).__name__}")
    elif queue not in tax.queues:
        errors.append(f"Invalid queue '{queue}'. Must be one of {list(tax.queues)}")

    # 5. decision <-> queue consistency
    if decision in ("auto_rag", "reject_oos") and queue != "none":
        errors.append(
            f"Inconsistent decision/queue: decision '{decision}' requires queue 'none', got '{queue}'"
        )
    elif decision == "escalate" and queue == "none":
        errors.append(
            "Inconsistent decision/queue: decision 'escalate' cannot have queue 'none'"
        )

    # 5b. decision <-> intent consistency
    if decision == "reject_oos" and intent != "other":
        errors.append(
            f"Inconsistent decision/intent: decision 'reject_oos' requires intent 'other', got '{intent}'"
        )

    # 6. escalate bool consistency
    escalate = d.get("escalate")
    if escalate is None:
        errors.append("Field 'escalate' is required")
    elif not isinstance(escalate, bool):
        errors.append(
            f"Field 'escalate' must be a boolean, got {type(escalate).__name__}"
        )
    else:
        if escalate is True and decision != "escalate":
            errors.append(
                f"Inconsistent escalate flag: escalate=True requires decision='escalate', got '{decision}'"
            )
        elif escalate is False and decision not in ("auto_rag", "reject_oos"):
            errors.append(
                f"Inconsistent escalate flag: escalate=False requires decision in ('auto_rag', 'reject_oos'), got '{decision}'"
            )

    # 7. sentiment validation (optional)
    sentiment = d.get("sentiment")
    if sentiment is not None:
        if not isinstance(sentiment, str):
            errors.append(
                f"Field 'sentiment' must be a string or None, got {type(sentiment).__name__}"
            )
        elif sentiment not in tax.sentiments:
            errors.append(
                f"Invalid sentiment '{sentiment}'. Must be in {list(tax.sentiments)}"
            )

    # 8. urgency validation (optional)
    urgency = d.get("urgency")
    if urgency is not None:
        if not isinstance(urgency, int) or isinstance(urgency, bool):
            errors.append(
                f"Field 'urgency' must be an integer (0..3) or None, got {type(urgency).__name__}"
            )
        elif urgency not in tax.urgency_levels:
            errors.append(
                f"Invalid urgency level {urgency}. Must be in {list(tax.urgency_levels.keys())}"
            )

    return errors
