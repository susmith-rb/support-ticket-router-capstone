"""Public dataset source adapter for Bitext Customer Support.

# REQ-REVAL-004: Source adapters: public Bitext
Provides:
1. Placeholder rendering for Bitext entity tokens (e.g. {{Order Number}} -> [ORDER]).
2. Flag to register and scenario mapping (Q->casual, Z->noisy, K->terse, P->formal, W->emotional).
3. Taxonomy mapping table loading from mappings/bitext_to_taxonomy.yaml.
4. Deterministic stratified sampling over (intent, flag-style).
"""

from __future__ import annotations

import csv
import pathlib
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from evals.routing.schema import (
    Annotation,
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
)

_MODULE_DIR = pathlib.Path(__file__).resolve().parent
_DEFAULT_MAPPING_PATH = _MODULE_DIR.parent / "mappings" / "bitext_to_taxonomy.yaml"
_DEFAULT_BITEXT_CSV = pathlib.Path(".cache/routing_eval/raw/bitext/Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv")

PLACEHOLDER_REPLACEMENTS: Dict[str, str] = {
    "{{Order Number}}": "[ORDER]",
    "{{Invoice Number}}": "[INVOICE]",
    "{{Online Order Interaction}}": "order online",
    "{{Online Payment Interaction}}": "online payment",
    "{{Online Navigation Step}}": "navigation menu",
    "{{Online Customer Support Channel}}": "customer support",
    "{{Profile}}": "my profile",
    "{{Profile Type}}": "personal profile",
    "{{Settings}}": "account settings",
    "{{Online Company Portal Info}}": "company portal",
    "{{Date}}": "yesterday",
    "{{Date Range}}": "last week",
    "{{Shipping Cut-off Time}}": "5 PM",
    "{{Delivery City}}": "Seattle",
    "{{Delivery Country}}": "United States",
    "{{Salutation}}": "Hello",
    "{{Client First Name}}": "Alex",
    "{{Client Last Name}}": "Taylor",
    "{{Customer Support Phone Number}}": "[PHONE]",
    "{{Customer Support Email}}": "[EMAIL]",
    "{{Live Chat Support}}": "live chat",
    "{{Website URL}}": "[URL]",
    "{{Upgrade Account}}": "upgrade account",
    "{{Account Type}}": "standard account",
    "{{Account Category}}": "retail account",
    "{{Account Change}}": "switch profile",
    "{{Program}}": "membership program",
    "{{Refund Amount}}": "$50",
    "{{Money Amount}}": "$50",
    "{{Store Location}}": "downtown store",
}


def render_placeholders(text: str) -> str:
    """Render Bitext placeholders to standard redaction tokens or natural phrases."""
    out = text
    for placeholder, replacement in PLACEHOLDER_REPLACEMENTS.items():
        if placeholder in out:
            out = out.replace(placeholder, replacement)
    return out


def map_flags_to_register(flags: str) -> str:
    """Map Bitext language generation flags to canonical register.

    Q -> casual
    Z -> casual (and noisy scenario)
    K -> terse
    P -> formal
    W -> emotional
    """
    f = set(flags)
    if "W" in f:
        return "emotional"
    if "K" in f:
        return "terse"
    if "P" in f and "Q" not in f and "Z" not in f:
        return "formal"
    if "Q" in f or "Z" in f:
        return "casual"
    return "casual"


def map_flags_to_scenarios(flags: str, extra_scenarios: Sequence[str] = ()) -> Tuple[str, ...]:
    """Map Bitext flags and extra scenarios to canonical scenario tuple."""
    scenarios: List[str] = []
    if "Z" in flags:
        scenarios.append("noisy")
    for extra in extra_scenarios:
        if extra not in scenarios:
            scenarios.append(extra)
    if not scenarios:
        scenarios.append("normal")
    return tuple(scenarios)


def compute_length_bucket(text: str) -> str:
    """Compute length bucket from whitespace-split token count."""
    count = len(text.split())
    if count < 20:
        return "short (<20 words)"
    if count <= 80:
        return "medium (20–80)"
    return "long (>80)"


def compute_difficulty(flags: str, scenarios: Sequence[str]) -> str:
    """Derive difficulty level from flags and scenarios."""
    if "noisy" in scenarios or "ood" in scenarios or "W" in flags:
        return "hard"
    if "K" in flags or "ambiguous" in scenarios:
        return "medium"
    return "easy"


def load_mapping(path: Optional[pathlib.Path | str] = None) -> Dict[str, Any]:
    """Load the YAML mapping file from bitext intents to routing taxonomy."""
    p = pathlib.Path(path) if path else _DEFAULT_MAPPING_PATH
    if not p.exists():
        raise FileNotFoundError(f"Bitext mapping file not found at: {p}")
    with p.open("r", encoding="utf-8") as f:
        data: Dict[str, Any] = yaml.safe_load(f) or {}
    return data.get("intents", {})


def sample(
    csv_path: pathlib.Path | str,
    n: int,
    seed: int = 20261028,
    per_intent_min: int = 10,
    mapping_path: Optional[pathlib.Path | str] = None,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Sample Bitext dataset with deterministic stratification across (intent, register).

    Args:
        csv_path: Path to Bitext CSV.
        n: Total number of rows to sample.
        seed: Random seed for deterministic sampling.
        per_intent_min: Minimum number of rows to allocate per Bitext intent.
        mapping_path: Path to bitext_to_taxonomy.yaml.
        dataset_version: Version string to attach to cases.

    Returns:
        List of RoutingCase objects in pool split.
    """
    path = pathlib.Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Bitext CSV not found at: {path}")

    mappings = load_mapping(mapping_path)

    # Read and group rows by (bitext_intent, register)
    rows_by_stratum: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            b_intent = row.get("intent", "").strip()
            flags = row.get("flags", "").strip()
            register = map_flags_to_register(flags)
            stratum = (b_intent, register)
            rows_by_stratum.setdefault(stratum, []).append(row)

    # Shuffle each stratum deterministically
    rng = random.Random(seed)
    for stratum in sorted(rows_by_stratum.keys()):
        rng.shuffle(rows_by_stratum[stratum])

    # Stratified selection
    unique_intents = sorted({intent for (intent, _) in rows_by_stratum.keys()})
    sampled_rows: List[Dict[str, str]] = []

    # 1. Guarantee per_intent_min across strata for each intent
    for intent in unique_intents:
        intent_strata = [s for s in sorted(rows_by_stratum.keys()) if s[0] == intent]
        allocated = 0
        s_idx = 0
        while allocated < per_intent_min and any(rows_by_stratum[s] for s in intent_strata):
            stratum = intent_strata[s_idx % len(intent_strata)]
            s_idx += 1
            if rows_by_stratum[stratum]:
                sampled_rows.append(rows_by_stratum[stratum].pop(0))
                allocated += 1

    # 2. Fill remainder up to n by round-robin across all non-empty strata
    all_strata = [s for s in sorted(rows_by_stratum.keys()) if rows_by_stratum[s]]
    s_idx = 0
    while len(sampled_rows) < n and any(rows_by_stratum[s] for s in all_strata):
        stratum = all_strata[s_idx % len(all_strata)]
        s_idx += 1
        if rows_by_stratum[stratum]:
            sampled_rows.append(rows_by_stratum[stratum].pop(0))

    # Deterministic sort by instruction content
    sampled_rows.sort(key=lambda r: (r.get("intent", ""), r.get("instruction", "")))

    # Convert to RoutingCase
    cases: List[RoutingCase] = []
    for idx, row in enumerate(sampled_rows):
        b_intent = row.get("intent", "").strip()
        flags = row.get("flags", "").strip()
        raw_instruction = row.get("instruction", "").strip()
        rendered_instruction = render_placeholders(raw_instruction)

        map_info = mappings.get(b_intent)
        if not map_info:
            raise KeyError(f"Bitext intent '{b_intent}' has no mapping in taxonomy")

        target_intent = map_info["intent"]
        acceptable_intents = tuple(map_info.get("acceptable_intents", [target_intent]))
        decision = map_info["decision"]
        queue = map_info["queue"]
        escalate = (decision == "escalate")
        scenarios_extra = map_info.get("scenarios_extra", [])
        rationale = map_info.get("rationale", "")

        register = map_flags_to_register(flags)
        length_bucket = compute_length_bucket(rendered_instruction)
        scenarios = map_flags_to_scenarios(flags, scenarios_extra)
        difficulty = compute_difficulty(flags, scenarios)

        # Sentiment heuristic for offensive/angry flag W
        sentiment = "angry" if "W" in flags else "neutral"
        urgency = 2 if "W" in flags else 1

        expected = Expected(
            intent=target_intent,
            acceptable_intents=acceptable_intents,
            escalate=escalate,
            decision=decision,
            queue=queue,
            sentiment=sentiment,
            urgency=urgency,
        )

        c_hash = compute_content_hash(subject=None, description=rendered_instruction)
        t_id = make_ticket_id("bitext_cs", c_hash)

        annotation = Annotation(
            annotator_id="bitext_mapping_v1",
            annotator_type="mapping",
            labels=expected.to_dict(),
            confidence=0.95,
            rationale=rationale,
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
            description=rendered_instruction,
            channel="chat",
            language="en",
            pii_scrubbed=True,
            expected=expected,
            difficulty=difficulty,
            scenarios=scenarios,
            style=Style(register=register, length_bucket=length_bucket),
            source="public",
            source_dataset="bitext_cs",
            source_ref=f"bitext_row_{idx}",
            ground_truth_source="mapped_public",
            label_status="provisional",
            verified_fields=(),
            annotations=(annotation,),
            adjudication=None,
            group_id=f"grp-{t_id}",
            parent_ticket_id=None,
            perturbation=None,
            failure_ref=None,
            notes=f"bitext_flags:{flags}",
        )
        cases.append(case)

    cases.sort(key=lambda c: c.ticket_id)
    return cases
