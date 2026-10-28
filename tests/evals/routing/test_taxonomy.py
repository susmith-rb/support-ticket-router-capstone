"""Unit tests and drift guards for routing taxonomy and expected behaviour validation.

# REQ-REVAL-001: Taxonomy single source of truth & label validation.
"""

from __future__ import annotations

import pathlib

import pytest

from ticket_router.config.taxonomy import (
    IntentDefinition,
    Taxonomy,
    is_valid_intent,
    load_taxonomy,
    validate_expected,
)


def test_taxonomy_loading_and_structure():
    """# REQ-REVAL-001: load_taxonomy loads cached, frozen dataclass with complete definitions."""
    tax1 = load_taxonomy()
    tax2 = load_taxonomy()
    assert tax1 is tax2, "load_taxonomy should be cached"
    assert isinstance(tax1, Taxonomy)

    expected_intents = (
        "order_status",
        "shipping_policy",
        "returns_refunds",
        "account_payment",
        "other",
    )
    assert tax1.intents == expected_intents

    for intent in expected_intents:
        assert intent in tax1.intent_definitions
        defn = tax1.intent_definitions[intent]
        assert isinstance(defn, IntentDefinition)
        assert len(defn.definition) > 10
        assert len(defn.positive_examples) >= 2
        assert len(defn.boundary_notes) > 5

    assert tax1.sentiments == ("angry", "frustrated", "neutral", "positive")
    assert tax1.urgency_levels == {
        0: "can wait days",
        1: "within a day",
        2: "within hours",
        3: "immediate",
    }
    assert set(tax1.decisions) == {"auto_rag", "escalate"}
    assert set(tax1.queues) == {"none", "human-urgent", "human-standard", "human-low"}
    assert set(tax1.tiers) == {
        "tier0_rules",
        "tier1_router_s1",
        "tier2_flash",
        "model_armor",
        "fallback_degraded",
    }

    assert len(tax1.expected_behaviour.escalate_triggers) >= 5
    assert len(tax1.expected_behaviour.queue_rules) >= 3
    assert len(tax1.expected_behaviour.decision_rules) >= 3


def test_is_valid_intent():
    """# REQ-REVAL-001: is_valid_intent correctly checks intent membership."""
    assert is_valid_intent("order_status") is True
    assert is_valid_intent("shipping_policy") is True
    assert is_valid_intent("returns_refunds") is True
    assert is_valid_intent("account_payment") is True
    assert is_valid_intent("other") is True

    assert is_valid_intent("billing") is False
    assert is_valid_intent("technical") is False
    assert is_valid_intent("order") is False
    assert is_valid_intent("") is False


def test_validate_expected_success():
    """# REQ-REVAL-001: validate_expected succeeds on well-formed expected labels."""
    valid_auto = {
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
        "sentiment": "neutral",
        "urgency": 1,
    }
    assert validate_expected(valid_auto) == []

    valid_multi_intent = {
        "intent": "returns_refunds",
        "acceptable_intents": ["returns_refunds", "order_status"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
    }
    assert validate_expected(valid_multi_intent) == []

    valid_escalate = {
        "intent": "account_payment",
        "acceptable_intents": ["account_payment"],
        "escalate": True,
        "decision": "escalate",
        "queue": "human-standard",
        "sentiment": "angry",
        "urgency": 3,
    }
    assert validate_expected(valid_escalate) == []

    valid_urgent = {
        "intent": "other",
        "acceptable_intents": ["other"],
        "escalate": True,
        "decision": "escalate",
        "queue": "human-urgent",
        "sentiment": "angry",
        "urgency": 3,
    }
    assert validate_expected(valid_urgent) == []


def test_validate_expected_failures():
    """# REQ-REVAL-001: validate_expected detects invalid intents, inconsistent decisions, and invalid queues."""
    # Invalid primary intent
    assert any("Invalid intent 'billing'" in e for e in validate_expected({
        "intent": "billing",
        "acceptable_intents": ["billing"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
    }))

    # Acceptable intents does not contain primary intent
    assert any("must contain primary intent" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["returns_refunds"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
    }))

    # Decision and queue mismatch: auto_rag with queue != none
    assert any("requires queue 'none'" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "human-standard",
    }))

    # Decision and queue mismatch: escalate with queue == none
    assert any("cannot have queue 'none'" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": True,
        "decision": "escalate",
        "queue": "none",
    }))

    # Escalate bool mismatch
    assert any("escalate=True requires decision='escalate'" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": True,
        "decision": "auto_rag",
        "queue": "none",
    }))

    # Invalid sentiment
    assert any("Invalid sentiment 'furious'" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
        "sentiment": "furious",
    }))

    # Invalid urgency
    assert any("Invalid urgency level 5" in e for e in validate_expected({
        "intent": "order_status",
        "acceptable_intents": ["order_status"],
        "escalate": False,
        "decision": "auto_rag",
        "queue": "none",
        "urgency": 5,
    }))


def test_drift_guard_vs_router_schema_and_search_client():
    """# REQ-REVAL-001: Drift guard asserts YAML agrees with router_schema.py, search_client, and QueuePriority."""
    tax = load_taxonomy()

    # 1. Check against training/router_s1/pipeline/router_schema.py
    try:
        from training.router_s1.pipeline.router_schema import (
            INTENTS as S1_INTENTS,
        )
        from training.router_s1.pipeline.router_schema import (
            QUESTIONS as S1_QUESTIONS,
        )
        from training.router_s1.pipeline.router_schema import (
            SENTIMENTS as S1_SENTIMENTS,
        )

        assert list(tax.intents) == S1_INTENTS, "Taxonomy intents drifted from router_schema.INTENTS"
        assert list(tax.sentiments) == S1_SENTIMENTS, "Taxonomy sentiments drifted from router_schema.SENTIMENTS"
        s1_urgency_names = list(S1_QUESTIONS["urgency"]["criteria"])
        tax_urgency_names = [tax.urgency_levels[i] for i in range(4)]
        assert tax_urgency_names == s1_urgency_names, "Taxonomy urgency drifted from router_schema urgency criteria"
    except ImportError as e:
        pytest.fail(f"Could not import router_schema for drift check: {e}")

    # 2. Check against ticket_router.retrieval.search_client
    from ticket_router.retrieval.search_client import VALID_INTENT_FAMILIES

    assert set(tax.intents) == set(VALID_INTENT_FAMILIES), "Taxonomy intents drifted from search_client.VALID_INTENT_FAMILIES"

    # 3. Check against policy.QueuePriority
    from ticket_router.agents.triage_router.policy import QueuePriority

    priority_queues = {QueuePriority.P1_URGENT.value, QueuePriority.P2_STANDARD.value, QueuePriority.P3_LOW.value}
    tax_human_queues = {q for q in tax.queues if q != "none"}
    assert priority_queues == tax_human_queues, "Taxonomy queues drifted from QueuePriority enum"


def test_production_code_does_not_import_taxonomy():
    """# REQ-REVAL-001: Invariant: production code in ticket_router must NOT import taxonomy."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    src_ticket_router = repo_root / "src" / "ticket_router"

    offenders = []
    for py_file in src_ticket_router.rglob("*.py"):
        if "config/taxonomy.py" in str(py_file):
            continue
        content = py_file.read_text(encoding="utf-8")
        if "ticket_router.config.taxonomy" in content or "from ticket_router.config import taxonomy" in content:
            offenders.append(str(py_file.relative_to(repo_root)))

    assert not offenders, f"Production code must NOT import taxonomy: {offenders}"
