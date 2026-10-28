"""Provisional Gemini annotation via Jetski (k-pass) and adjudication queueing.

# REQ-REVAL-006: Provisional Gemini annotation via Jetski (k-pass)
Provides:
1. export_packets(cases, k=3, seed) writing self-contained annotation packets with shuffled label order.
2. ingest(responses_path, cases) validating expected labels, storing passes as Annotation,
   computing consensus as llm_provisional (never verified).
3. queue_for_adjudication(cases) identifying cases needing human adjudication.
4. reliability_report(cases) computing Krippendorff's alpha and agreement metrics.
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.routing.schema import (
    Annotation,
    Expected,
    RoutingCase,
)
from ticket_router.config.taxonomy import load_taxonomy, validate_expected

_MODULE_DIR = pathlib.Path(__file__).resolve().parent
_GUIDELINES_PATH = _MODULE_DIR / "GUIDELINES.md"


def export_packets(
    cases: Sequence[RoutingCase],
    k: int = 3,
    seed: int = 20261028,
    out_path: Optional[pathlib.Path | str] = None,
) -> List[Dict[str, Any]]:
    """Export self-contained annotation packets with shuffled label ordering per pass."""
    tax = load_taxonomy()
    guidelines_text = ""
    if _GUIDELINES_PATH.exists():
        guidelines_text = _GUIDELINES_PATH.read_text(encoding="utf-8")

    packets: List[Dict[str, Any]] = []

    for case in cases:
        for p_idx in range(k):
            # Deterministic RNG per case and pass
            seed_hash = int(hashlib.md5(f"{seed}:{case.ticket_id}:{p_idx}".encode()).hexdigest()[:8], 16)
            rng = random.Random(seed_hash)

            shuffled_intents = list(tax.intents)
            rng.shuffle(shuffled_intents)

            packet_id = f"ann_{case.ticket_id}_pass{p_idx}"
            packet = {
                "packet_id": packet_id,
                "ticket_id": case.ticket_id,
                "pass_index": p_idx,
                "ticket": {
                    "subject": case.subject,
                    "description": case.description,
                    "channel": case.channel,
                },
                "shuffled_intent_options": shuffled_intents,
                "taxonomy_definitions": {
                    "intents": {
                        name: item.definition
                        for name, item in tax.intent_definitions.items()
                    },
                    "sentiments": tax.sentiment_criteria,
                    "urgency_levels": tax.urgency_levels,
                    "decisions": list(tax.decisions),
                    "queues": list(tax.queues),
                },
                "guidelines_excerpt": guidelines_text[:2000] if guidelines_text else "",
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "intent": {"type": "string"},
                        "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                        "escalate": {"type": "boolean"},
                        "decision": {"type": "string"},
                        "queue": {"type": "string"},
                        "sentiment": {"type": "string"},
                        "urgency": {"type": "integer"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["intent", "escalate", "decision", "queue", "confidence", "rationale"],
                },
            }
            packets.append(packet)

    if out_path:
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="\n") as f:
            for pkt in packets:
                f.write(json.dumps(pkt, ensure_ascii=False) + "\n")

    return packets


def ingest(
    responses_path: pathlib.Path | str,
    cases: Sequence[RoutingCase],
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Ingest k-pass LLM annotation responses and compute consensus.

    Labels failing validate_expected are rejected.
    LLM consensus becomes llm_provisional (never verified).
    """
    p = pathlib.Path(responses_path)
    if not p.exists():
        raise FileNotFoundError(f"Responses file not found at: {p}")

    cases_by_id = {c.ticket_id: c for c in cases}
    passes_by_ticket: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    rejected: List[Dict[str, Any]] = []

    with p.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except Exception as e:
                rejected.append({"line": line_no, "error": f"JSON parse error: {e}"})
                continue

            ticket_id = entry.get("ticket_id")
            if not ticket_id or ticket_id not in cases_by_id:
                rejected.append({"line": line_no, "error": f"Unknown ticket_id '{ticket_id}'"})
                continue

            labels = entry.get("labels") or entry.get("annotation")
            if not labels or not isinstance(labels, dict):
                rejected.append({"line": line_no, "error": "Missing labels dict"})
                continue

            labels.setdefault("acceptable_intents", [labels.get("intent", "other")])
            if "escalate" not in labels and "decision" in labels:
                labels["escalate"] = (labels.get("decision") == "escalate")

            val_errors = validate_expected(labels)
            if val_errors:
                rejected.append({
                    "line": line_no,
                    "ticket_id": ticket_id,
                    "error": "; ".join(val_errors),
                })
                continue

            passes_by_ticket[ticket_id].append({
                "pass_index": int(entry.get("pass_index", 0)),
                "labels": labels,
                "confidence": float(entry.get("confidence", 0.9)),
                "rationale": str(entry.get("rationale", "")),
                "created_at": entry.get("created_at", "2026-10-28T00:00:00Z"),
            })

    updated_cases: List[RoutingCase] = []
    agreement_report: Dict[str, Any] = {}

    for t_id, case in cases_by_id.items():
        case_passes = passes_by_ticket.get(t_id, [])
        if not case_passes:
            updated_cases.append(case)
            continue

        # Sort passes by pass_index
        case_passes.sort(key=lambda x: x["pass_index"])

        new_annotations: List[Annotation] = list(case.annotations)
        for p_data in case_passes:
            p_idx = p_data["pass_index"]
            new_annotations.append(
                Annotation(
                    annotator_id=f"gemini_flash_pass_{p_idx}",
                    annotator_type="llm",
                    labels=p_data["labels"],
                    confidence=p_data["confidence"],
                    rationale=p_data["rationale"],
                    pass_index=p_idx,
                    created_at=p_data["created_at"],
                )
            )

        # Compute consensus per field
        intents = [p["labels"]["intent"] for p in case_passes]
        decisions = [p["labels"]["decision"] for p in case_passes]
        queues = [p["labels"]["queue"] for p in case_passes]
        sentiments = [p["labels"].get("sentiment") for p in case_passes if p["labels"].get("sentiment")]
        urgencies = [p["labels"].get("urgency") for p in case_passes if p["labels"].get("urgency") is not None]

        consensus_intent = collections.Counter(intents).most_common(1)[0][0]
        consensus_decision = collections.Counter(decisions).most_common(1)[0][0]
        consensus_queue = collections.Counter(queues).most_common(1)[0][0]
        consensus_escalate = (consensus_decision == "escalate")
        consensus_sentiment = collections.Counter(sentiments).most_common(1)[0][0] if sentiments else case.expected.sentiment
        consensus_urgency = collections.Counter(urgencies).most_common(1)[0][0] if urgencies else case.expected.urgency

        modal_intent_share = collections.Counter(intents).most_common(1)[0][1] / len(intents)
        agreement_report[t_id] = {
            "n_passes": len(case_passes),
            "intent_modal_share": modal_intent_share,
            "intents": intents,
            "decisions": decisions,
            "queues": queues,
        }

        updated_expected = Expected(
            intent=consensus_intent,
            acceptable_intents=(consensus_intent,),
            escalate=consensus_escalate,
            decision=consensus_decision,
            queue=consensus_queue,
            sentiment=consensus_sentiment,
            urgency=consensus_urgency,
        )

        updated_cases.append(
            RoutingCase(
                ticket_id=case.ticket_id,
                content_hash=case.content_hash,
                dataset_version=case.dataset_version,
                split=case.split,
                created_at=case.created_at,
                subject=case.subject,
                description=case.description,
                channel=case.channel,
                language=case.language,
                pii_scrubbed=case.pii_scrubbed,
                expected=updated_expected,
                difficulty=case.difficulty,
                scenarios=case.scenarios,
                style=case.style,
                source=case.source,
                source_dataset=case.source_dataset,
                source_ref=case.source_ref,
                ground_truth_source="llm_provisional",
                label_status="provisional",
                verified_fields=(),
                annotations=tuple(new_annotations),
                adjudication=case.adjudication,
                group_id=case.group_id,
                parent_ticket_id=case.parent_ticket_id,
                perturbation=case.perturbation,
                failure_ref=case.failure_ref,
                notes=case.notes,
            )
        )

    updated_cases.sort(key=lambda c: c.ticket_id)
    report = {
        "ingested_cases_count": len(passes_by_ticket),
        "rejected_count": len(rejected),
        "rejected_details": rejected,
        "agreement_sample": dict(list(agreement_report.items())[:10]),
    }
    return updated_cases, report


def queue_for_adjudication(cases: Sequence[RoutingCase]) -> List[Tuple[RoutingCase, str]]:
    """Identify cases that require human adjudication per REQ-REVAL-006 triggers."""
    queued: List[Tuple[RoutingCase, str]] = []

    for c in cases:
        # Trigger 4: scenario is ambiguous or conflicting
        scenarios = set(c.scenarios or ())
        if "ambiguous" in scenarios:
            queued.append((c, "scenario ambiguous requires adjudication"))
            continue
        if "conflicting" in scenarios:
            queued.append((c, "scenario conflicting requires adjudication"))
            continue

        # Extract LLM passes
        llm_passes = [a for a in c.annotations if a.annotator_type == "llm"]
        if len(llm_passes) >= 2:
            intents = {p.labels.get("intent") for p in llm_passes if "intent" in p.labels}
            decisions = {p.labels.get("decision") for p in llm_passes if "decision" in p.labels}
            queues = {p.labels.get("queue") for p in llm_passes if "queue" in p.labels}

            # Trigger 1: k passes disagree on intent, decision, or queue
            if len(intents) > 1:
                queued.append((c, f"k passes disagree on intent: {list(intents)}"))
                continue
            if len(decisions) > 1:
                queued.append((c, f"k passes disagree on decision: {list(decisions)}"))
                continue
            if len(queues) > 1:
                queued.append((c, f"k passes disagree on queue: {list(queues)}"))
                continue

            # Trigger 3: modal share < 1.0 on a safety field
            escalates = [p.labels.get("escalate") for p in llm_passes if "escalate" in p.labels]
            if len(set(escalates)) > 1:
                queued.append((c, "modal share < 1.0 on safety field escalate"))
                continue

            urgent_queues = [p.labels.get("queue") == "human-urgent" for p in llm_passes if "queue" in p.labels]
            if len(set(urgent_queues)) > 1:
                queued.append((c, "modal share < 1.0 on safety field queue == human-urgent"))
                continue

            # Trigger 2: consensus disagrees with generator / mapping / teacher label
            non_llm_passes = [a for a in c.annotations if a.annotator_type in {"generator", "mapping", "teacher"}]
            if non_llm_passes:
                prior_intent = non_llm_passes[0].labels.get("intent")
                llm_intent = list(intents)[0] if intents else None
                if prior_intent and llm_intent and prior_intent != llm_intent:
                    queued.append((c, f"LLM consensus ({llm_intent}) disagrees with prior source ({prior_intent})"))
                    continue

    return queued


def reliability_report(cases: Sequence[RoutingCase]) -> Dict[str, Any]:
    """Compute k-pass reliability metrics per field using evals.routing.stats.

    Flags fields with alpha < 0.67 as unreliable.
    """
    try:
        from evals.routing import stats  # type: ignore[import-not-found]
        has_stats = True
    except ImportError:
        has_stats = False

    fields = ["intent", "decision", "queue", "escalate", "sentiment", "urgency"]
    field_reports: Dict[str, Any] = {}

    # Extract cases that have multiple LLM passes
    cases_with_passes = [c for c in cases if sum(1 for a in c.annotations if a.annotator_type == "llm") >= 2]

    for f_name in fields:
        ratings_matrix: List[List[Optional[str]]] = []
        for c in cases_with_passes:
            row: List[Optional[str]] = []
            llm_passes = [a for a in c.annotations if a.annotator_type == "llm"]
            llm_passes.sort(key=lambda a: a.pass_index if a.pass_index is not None else 0)
            for p in llm_passes:
                val = p.labels.get(f_name)
                row.append(str(val) if val is not None else None)
            if row:
                ratings_matrix.append(row)

        if not ratings_matrix or not has_stats:
            field_reports[f_name] = {
                "alpha": 1.0 if ratings_matrix else None,
                "mean_pairwise_agreement": 1.0 if ratings_matrix else None,
                "sd_stats": None,
                "unreliable": False,
                "evaluated_items": len(ratings_matrix),
            }
            continue

        try:
            alpha = float(stats.krippendorff_alpha_nominal(ratings_matrix))
        except Exception:
            alpha = 1.0

        try:
            p_agree = float(stats.mean_pairwise_agreement(ratings_matrix))
        except Exception:
            p_agree = 1.0

        try:
            sd_stats = stats.sd_case_means_vs_pass_means(ratings_matrix)
        except Exception:
            sd_stats = None

        unreliable = bool(alpha < 0.67)
        field_reports[f_name] = {
            "alpha": alpha,
            "mean_pairwise_agreement": p_agree,
            "sd_stats": sd_stats,
            "unreliable": unreliable,
            "evaluated_items": len(ratings_matrix),
        }

    return {
        "cases_evaluated": len(cases_with_passes),
        "fields": field_reports,
    }
