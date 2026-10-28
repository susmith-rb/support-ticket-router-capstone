"""Synthetic data generator, generation packet exporter, and ingestion adapter.

# REQ-REVAL-004: Source adapters: synthetic data
Provides:
1. Deterministic template generator for structural scenarios (missing_info, conflicting, rare, long, ood).
2. Default coverage spec builder filling intent x scenario x register x length.
3. Generation packet exporter for offline Jetski agent creation.
4. Generated ticket ingestion adapter validating against taxonomy rules.
"""

from __future__ import annotations

import json
import pathlib
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.routing.schema import (
    Annotation,
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
)
from ticket_router.config.taxonomy import load_taxonomy, validate_expected

_MODULE_DIR = pathlib.Path(__file__).resolve().parent
_GUIDELINES_PATH = _MODULE_DIR.parent / "GUIDELINES.md"


# Deterministic templates for structural scenario coverage
_TEMPLATE_BANK: Dict[str, List[Dict[str, Any]]] = {
    "missing_info": [
        {
            "description": "not working ?",
            "intent": "other",
            "acceptable_intents": ["other"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "terse",
            "difficulty": "hard",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Vague missing info query per Rule 6",
        },
        {
            "description": "where order ?",
            "intent": "order_status",
            "acceptable_intents": ["order_status"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "terse",
            "difficulty": "medium",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Terse order tracking with minimal words",
        },
        {
            "description": "package status please",
            "intent": "order_status",
            "acceptable_intents": ["order_status"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "terse",
            "difficulty": "easy",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Short status request without order ID",
        },
        {
            "description": "refund ?",
            "intent": "returns_refunds",
            "acceptable_intents": ["returns_refunds"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "terse",
            "difficulty": "medium",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Terse refund inquiry",
        },
    ],
    "conflicting": [
        {
            "description": "I wanted to check the delivery date for order [ORDER], but actually please cancel the order immediately instead.",
            "intent": "order_status",
            "acceptable_intents": ["order_status"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "casual",
            "difficulty": "hard",
            "sentiment": "neutral",
            "urgency": 2,
            "notes": "Conflicting statement resolved by most recent explicit request (cancel) per Rule 5",
        },
        {
            "description": "I need to return this sweater because it is too large, but wait, do you have express shipping if I reorder in size medium?",
            "intent": "shipping_policy",
            "acceptable_intents": ["shipping_policy", "returns_refunds"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "casual",
            "difficulty": "hard",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Return question followed by shipping policy inquiry; recent statement asks about shipping",
        },
        {
            "description": "I was checking my invoice for this month, but my card was charged fraudulently and someone stole my account!",
            "intent": "account_payment",
            "acceptable_intents": ["account_payment"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "emotional",
            "difficulty": "hard",
            "sentiment": "angry",
            "urgency": 3,
            "notes": "Invoice inquiry contains security emergency; Rule 1 escalation supersedes",
        },
    ],
    "rare": [
        {
            "description": "We are a registered 501(c)(3) nonprofit organization. Where do we upload our state sales tax exemption certificate for billing?",
            "intent": "account_payment",
            "acceptable_intents": ["account_payment"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "formal",
            "difficulty": "medium",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Tax exemption document submission for account billing",
        },
        {
            "description": "What are your international hazardous material customs declarations and restrictions for shipping lithium ion battery packs overseas?",
            "intent": "shipping_policy",
            "acceptable_intents": ["shipping_policy"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "formal",
            "difficulty": "hard",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Specialized international hazmat shipping policy inquiry",
        },
        {
            "description": "How do I request an estate inheritance transfer for a deceased relative's account balance and active digital subscriptions?",
            "intent": "account_payment",
            "acceptable_intents": ["account_payment", "other"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "formal",
            "difficulty": "hard",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Rare legal/estate account management workflow requiring human intervention",
        },
    ],
    "long": [
        {
            "description": "Hello customer care team, I am writing to you today regarding an order that I placed approximately ten days ago while I was traveling on an extended business trip across several states. I originally expected the parcel to be delivered to my home address before I returned, but when I arrived back yesterday afternoon and inspected my porch and mailbox, there was no sign of the delivery anywhere. I looked at the tracking link provided in the confirmation email and the courier status seems to be stuck indefinitely at the regional sorting facility without any timestamp updates over the last four business days. Could someone please look into this shipment and provide an updated estimated time of arrival?",
            "intent": "order_status",
            "acceptable_intents": ["order_status"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "formal",
            "difficulty": "medium",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Long narrative (>80 words) tracking query",
        },
        {
            "description": "Dear support, I recently purchased three different items from your online marketplace, including a winter jacket, waterproof hiking boots, and a matching fleece beanie. While the boots and beanie fit perfectly and arrived in great condition, the jacket unfortunately arrived in the wrong color and size entirely. I checked the packing slip inside the box and it lists a completely different product SKU than what I ordered on your web store. I would like to know the exact step-by-step return instructions for returning just the jacket for a replacement or store credit without affecting the discounts applied to the other two items from the promotional bundle.",
            "intent": "returns_refunds",
            "acceptable_intents": ["returns_refunds"],
            "escalate": False,
            "decision": "auto_rag",
            "queue": "none",
            "register": "formal",
            "difficulty": "medium",
            "sentiment": "neutral",
            "urgency": 1,
            "notes": "Long narrative return and exchange inquiry with complex bundle details",
        },
    ],
    "ood": [
        {
            "description": "Do you provide financial portfolio management services, stock trading advice, or cryptocurrency exchange options on your retail platform?",
            "intent": "other",
            "acceptable_intents": ["other"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "formal",
            "difficulty": "easy",
            "sentiment": "neutral",
            "urgency": 0,
            "notes": "Out of distribution commercial financial request",
        },
        {
            "description": "I would like to apply for an open software engineering position at your company headquarters. Where do I send my curriculum vitae?",
            "intent": "other",
            "acceptable_intents": ["other"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "formal",
            "difficulty": "easy",
            "sentiment": "neutral",
            "urgency": 0,
            "notes": "Out of distribution recruitment inquiry",
        },
        {
            "description": "Can someone give me directions to the nearest subway transit station near your main warehouse facility?",
            "intent": "other",
            "acceptable_intents": ["other"],
            "escalate": True,
            "decision": "escalate",
            "queue": "human-standard",
            "register": "casual",
            "difficulty": "easy",
            "sentiment": "neutral",
            "urgency": 0,
            "notes": "Out of distribution local navigation question",
        },
    ],
}


def _compute_length_bucket(text: str) -> str:
    count = len(text.split())
    if count < 20:
        return "short (<20 words)"
    if count <= 80:
        return "medium (20–80)"
    return "long (>80)"


def generate_templates(
    seed: int = 20261028,
    n_per_cell: int = 2,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Deterministically generate synthetic template cases for structural scenarios."""
    rng = random.Random(seed)
    cases: List[RoutingCase] = []

    for scenario, template_list in _TEMPLATE_BANK.items():
        # Deterministically sample templates
        shuffled = list(template_list)
        rng.shuffle(shuffled)
        selected = shuffled[: min(len(shuffled), max(1, n_per_cell))]

        for idx, t_data in enumerate(selected):
            desc = t_data["description"]
            intent = t_data["intent"]
            acceptable = tuple(t_data.get("acceptable_intents", [intent]))
            escalate = t_data["escalate"]
            decision = t_data["decision"]
            queue = t_data["queue"]
            sentiment = t_data.get("sentiment", "neutral")
            urgency = t_data.get("urgency", 1)

            expected = Expected(
                intent=intent,
                acceptable_intents=acceptable,
                escalate=escalate,
                decision=decision,
                queue=queue,
                sentiment=sentiment,
                urgency=urgency,
            )

            c_hash = compute_content_hash(subject=None, description=desc)
            t_id = make_ticket_id("synthetic_template", c_hash)

            style = Style(
                register=t_data.get("register", "casual"),
                length_bucket=_compute_length_bucket(desc),
            )

            ann = Annotation(
                annotator_id="template_generator_v1",
                annotator_type="generator",
                labels=expected.to_dict(),
                confidence=1.0,
                rationale=t_data.get("notes", "Template generator seed"),
                pass_index=0,
                created_at="2026-10-28T00:00:00Z",
            )

            case = RoutingCase(
                ticket_id=t_id,
                content_hash=c_hash,
                dataset_version=dataset_version,
                split="pool",
                created_at="2026-10-28T00:00:00Z",
                subject=None,
                description=desc,
                channel="web",
                language="en",
                pii_scrubbed=True,
                expected=expected,
                difficulty=t_data.get("difficulty", "medium"),
                scenarios=(scenario,),
                style=style,
                source="synthetic",
                source_dataset="synthetic_template",
                source_ref=f"template_{scenario}_{idx}",
                ground_truth_source="synthetic_intended",
                label_status="provisional",
                verified_fields=(),
                annotations=(ann,),
                adjudication=None,
                group_id=f"grp-{t_id}",
                parent_ticket_id=None,
                perturbation=None,
                failure_ref=None,
                notes=t_data.get("notes", ""),
            )
            cases.append(case)

    cases.sort(key=lambda c: c.ticket_id)
    return cases


def default_cells(target_total: int = 200) -> List[Dict[str, Any]]:
    """Build cell coverage specifications ensuring REQ-REVAL-004 targets are met."""
    tax = load_taxonomy()
    intents = list(tax.intents)
    scenarios = [
        "normal",
        "ambiguous",
        "missing_info",
        "noisy",
        "long",
        "rare",
        "ood",
        "conflicting",
        "prompt_injection",
    ]
    registers = ["formal", "casual", "terse", "emotional"]
    lengths = ["short (<20 words)", "medium (20–80)", "long (>80)"]
    difficulties = ["easy", "medium", "hard"]

    cells: List[Dict[str, Any]] = []
    cell_idx = 0

    # Ensure all scenarios and intents are represented
    for sc in scenarios:
        for it in intents:
            if sc == "ood" and it != "other":
                continue
            reg = registers[cell_idx % len(registers)]
            length = lengths[(cell_idx // len(registers)) % len(lengths)]
            diff = difficulties[(cell_idx // (len(registers) * len(lengths))) % len(difficulties)]

            cells.append(
                {
                    "cell_id": f"cell_{cell_idx:04d}",
                    "intent": it,
                    "scenario": sc,
                    "register": reg,
                    "length_bucket": length,
                    "difficulty": diff,
                    "n": 2,
                }
            )
            cell_idx += 1

    return cells


def export_generation_packets(
    spec_cells: Sequence[Dict[str, Any]],
    seed: int = 20261028,
    out_path: Optional[pathlib.Path | str] = None,
) -> List[Dict[str, Any]]:
    """Export self-contained generation packets for offline Jetski agent creation."""
    tax = load_taxonomy()
    guidelines_excerpt = ""
    if _GUIDELINES_PATH.exists():
        guidelines_excerpt = _GUIDELINES_PATH.read_text(encoding="utf-8")

    packets: List[Dict[str, Any]] = []
    rng = random.Random(seed)

    for cell in spec_cells:
        packet_id = f"gen_packet_{cell.get('cell_id', rng.randint(1000, 9999))}"
        packet = {
            "packet_id": packet_id,
            "target_cell": cell,
            "taxonomy": {
                "intents": list(tax.intents),
                "sentiments": list(tax.sentiments),
                "urgency_levels": tax.urgency_levels,
                "decisions": list(tax.decisions),
                "queues": list(tax.queues),
            },
            "guidelines_excerpt": guidelines_excerpt[:1500] if guidelines_excerpt else "",
            "output_schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": ["string", "null"]},
                        "description": {"type": "string"},
                        "intended_expected": {
                            "type": "object",
                            "properties": {
                                "intent": {"type": "string"},
                                "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                                "escalate": {"type": "boolean"},
                                "decision": {"type": "string"},
                                "queue": {"type": "string"},
                                "sentiment": {"type": "string"},
                                "urgency": {"type": "integer"},
                            },
                            "required": ["intent", "escalate", "decision", "queue"],
                        },
                        "notes": {"type": "string"},
                    },
                    "required": ["description", "intended_expected"],
                },
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


def ingest_generated(
    path: pathlib.Path | str,
    dataset_version: str = "routing_v1.0.0",
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Ingest synthetic generated cases from JSONL, rejecting invalid labels."""
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Generated cases file not found at: {p}")

    cases: List[RoutingCase] = []
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

            # Support both individual case JSON and wrapper packet with items
            items = entry.get("items") if isinstance(entry.get("items"), list) else [entry]

            for item in items:
                desc = item.get("description", "").strip()
                if not desc:
                    rejected.append({"item": item, "error": "Missing description"})
                    continue

                exp_dict = item.get("intended_expected") or item.get("expected")
                if not exp_dict or not isinstance(exp_dict, dict):
                    rejected.append({"item": item, "error": "Missing intended_expected dict"})
                    continue

                # Ensure defaults for required expected fields if missing
                exp_dict.setdefault("acceptable_intents", [exp_dict.get("intent", "other")])
                if "escalate" not in exp_dict:
                    exp_dict["escalate"] = (exp_dict.get("decision") == "escalate")

                val_errors = validate_expected(exp_dict)
                if val_errors:
                    rejected.append({"item": item, "error": "; ".join(val_errors)})
                    continue

                expected = Expected.from_dict(exp_dict)
                c_hash = compute_content_hash(subject=item.get("subject"), description=desc)
                t_id = make_ticket_id("synthetic_jetski", c_hash)

                cell_info = item.get("cell_info", {})
                scenarios = tuple(item.get("scenarios") or ([cell_info.get("scenario")] if cell_info.get("scenario") else ["normal"]))
                register = cell_info.get("register") or item.get("register") or "casual"
                length_bucket = _compute_length_bucket(desc)
                difficulty = cell_info.get("difficulty") or item.get("difficulty") or "medium"

                style = Style(register=register, length_bucket=length_bucket)

                ann = Annotation(
                    annotator_id="jetski_generator",
                    annotator_type="generator",
                    labels=expected.to_dict(),
                    confidence=float(item.get("confidence", 0.95)),
                    rationale=item.get("notes", "Synthetic intended label from generator"),
                    pass_index=0,
                    created_at="2026-10-28T00:00:00Z",
                )

                case = RoutingCase(
                    ticket_id=t_id,
                    content_hash=c_hash,
                    dataset_version=dataset_version,
                    split="pool",
                    created_at="2026-10-28T00:00:00Z",
                    subject=item.get("subject"),
                    description=desc,
                    channel=item.get("channel", "chat"),
                    language="en",
                    pii_scrubbed=True,
                    expected=expected,
                    difficulty=difficulty,
                    scenarios=scenarios,
                    style=style,
                    source="synthetic",
                    source_dataset="synthetic_jetski",
                    source_ref=item.get("source_ref", f"gen_{t_id}"),
                    ground_truth_source="synthetic_intended",
                    label_status="provisional",
                    verified_fields=(),
                    annotations=(ann,),
                    adjudication=None,
                    group_id=f"grp-{t_id}",
                    parent_ticket_id=None,
                    perturbation=None,
                    failure_ref=None,
                    notes=item.get("notes", ""),
                )
                cases.append(case)

    report = {
        "ingested_count": len(cases),
        "rejected_count": len(rejected),
        "rejected_details": rejected,
    }
    cases.sort(key=lambda c: c.ticket_id)
    return cases, report
