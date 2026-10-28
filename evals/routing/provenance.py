"""Ground-truth provenance and verification policy.

# REQ-REVAL-003: Ground-truth provenance & verification policy.
Defines provenance strength hierarchy, status derivation rules, and
provenance update helpers.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Sequence

from evals.routing.schema import (
    Adjudication,
    Annotation,
    RoutingCase,
)

# Provenance strength ordering (index 0 is highest/strongest)
PROVENANCE_STRENGTH: List[str] = [
    "human_adjudicated",
    "human_consensus",
    "human_single",
    "teacher_consensus",
    "mapped_public",
    "llm_provisional",
    "synthetic_intended",
    "rule_derived",
]

VALID_GROUND_TRUTH_SOURCES: tuple[str, ...] = tuple(PROVENANCE_STRENGTH)


def _check_annotations_disagreement(annotations: Sequence[Annotation | Dict[str, Any]]) -> bool:
    """Check if annotations disagree on intent, decision, or queue."""
    if len(annotations) < 2:
        return False

    intents: set[str] = set()
    decisions: set[str] = set()
    queues: set[str] = set()

    for ann in annotations:
        labels: Dict[str, Any] = {}
        if isinstance(ann, Annotation):
            labels = ann.labels
        elif isinstance(ann, dict):
            labels = ann.get("labels", {})

        if not labels:
            continue

        intent = labels.get("intent")
        if intent:
            intents.add(str(intent))

        decision = labels.get("decision")
        if decision:
            decisions.add(str(decision))

        queue = labels.get("queue")
        if queue:
            queues.add(str(queue))

    return len(intents) > 1 or len(decisions) > 1 or len(queues) > 1


def derive_status(case: RoutingCase | Dict[str, Any]) -> str:
    """Derive the canonical label_status according to verification policy.

    Rules:
    1. 'rejected' iff adjudication is present and verdict == 'reject'.
    2. 'verified' iff ground_truth_source in {'human_adjudicated', 'human_consensus'}
       and adjudication verdict != 'reject'.
    3. 'disputed' iff annotations disagree on intent, decision, or queue
       and no decisive adjudication exists (or adjudication verdict is 'unsure').
    4. Otherwise, 'provisional'.
    """
    if isinstance(case, RoutingCase):
        gt_source = case.ground_truth_source
        adjudication = case.adjudication
        annotations = case.annotations
    elif isinstance(case, dict):
        gt_source = case.get("ground_truth_source")
        raw_adj = case.get("adjudication")
        adjudication = (
            Adjudication.from_dict(raw_adj)
            if isinstance(raw_adj, dict)
            else raw_adj
        )
        raw_anns = case.get("annotations", [])
        annotations = [
            Annotation.from_dict(a) if isinstance(a, dict) else a
            for a in raw_anns
        ]
    else:
        raise TypeError(f"Expected RoutingCase or dict, got {type(case).__name__}")

    # 1. Adjudication rejection check
    if adjudication is not None and adjudication.verdict == "reject":
        return "rejected"

    # 2. Human verified check
    if gt_source in {"human_adjudicated", "human_consensus"}:
        return "verified"

    # 3. Disagreement check
    disagrees = _check_annotations_disagreement(annotations)
    if disagrees and (adjudication is None or adjudication.verdict == "unsure"):
        return "disputed"

    # 4. Default provisional
    return "provisional"


def update_provenance(case: RoutingCase) -> RoutingCase:
    """Recompute label_status for a RoutingCase and return an updated instance."""
    new_status = derive_status(case)
    if case.label_status == new_status:
        return case
    return dataclasses.replace(case, label_status=new_status)
