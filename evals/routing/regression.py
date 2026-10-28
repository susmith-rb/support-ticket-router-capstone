"""Regression dataset management, seed loader, and regression case recorder.

# REQ-REVAL-020: Versioned regression set of observed failures
Provides:
1. load_regression_seeds(yaml_path, adversarial_path) loading seeds from YAML and dev partition of adversarial_tone_v1.jsonl.
2. add_regression(text, symptom, root_cause, expected, observed_at, subject, seeds_path) appending newly observed failures.
3. Enforces failure_ref format REG-NNNN and rule_derived provisional status until adjudication.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

from evals.routing.schema import (
    Expected,
    FailureRef,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
)
from ticket_router.config.taxonomy import validate_expected

_MODULE_DIR = pathlib.Path(__file__).resolve().parent
_DEFAULT_SEEDS_PATH = _MODULE_DIR / "regression_seeds.yaml"
_DEFAULT_ADVERSARIAL_PATH = _MODULE_DIR.parent / "data" / "adversarial_tone_v1.jsonl"

_DOMAIN_TO_INTENT: Dict[str, str] = {
    "delivery_delay": "order_status",
    "return_policy": "returns_refunds",
    "damaged_goods": "returns_refunds",
    "wrong_item": "returns_refunds",
    "duplicate_charge": "account_payment",
    "stolen_package": "order_status",
    "spoiled_grocery": "returns_refunds",
    "order_cancellation": "order_status",
    "customs_duty": "shipping_policy",
    "warranty_claim": "returns_refunds",
    "gift_card_issue": "account_payment",
    "subscription_renewal": "account_payment",
    "missing_discount": "account_payment",
    "password_reset": "account_payment",
    "price_match": "account_payment",
    "missing_accessory": "returns_refunds",
    "out_of_stock": "order_status",
    "store_pickup": "shipping_policy",
    "hazmat_return": "shipping_policy",
    "account_security": "account_payment",
}


def _compute_length_bucket(text: str) -> str:
    count = len(text.split())
    if count < 20:
        return "short (<20 words)"
    if count <= 80:
        return "medium (20–80)"
    return "long (>80)"


def load_regression_seeds(
    yaml_path: Optional[pathlib.Path | str] = None,
    adversarial_path: Optional[pathlib.Path | str] = None,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Load curated regression cases from regression_seeds.yaml and adversarial dev seeds."""
    y_path = pathlib.Path(yaml_path) if yaml_path else _DEFAULT_SEEDS_PATH
    adv_path = pathlib.Path(adversarial_path) if adversarial_path else _DEFAULT_ADVERSARIAL_PATH

    cases: List[RoutingCase] = []
    seen_failure_ids: set[str] = set()

    # 1. Load curated seeds from YAML
    if y_path.exists():
        with y_path.open("r", encoding="utf-8") as f:
            raw_yaml = yaml.safe_load(f) or {}
        seed_entries = raw_yaml.get("seeds", [])

        for s in seed_entries:
            f_id = s["failure_id"]
            seen_failure_ids.add(f_id)
            desc = s["description"]
            subj = s.get("subject")
            exp_raw = s["expected"]

            val_errors = validate_expected(exp_raw)
            if val_errors:
                raise ValueError(f"Invalid expected dict in regression seed '{f_id}': {val_errors}")

            expected = Expected.from_dict(exp_raw)
            c_hash = compute_content_hash(subject=subj, description=desc)
            t_id = make_ticket_id("observed_failure", c_hash)

            fref = FailureRef(
                failure_id=f_id,
                observed_at=s.get("observed_at", "2026-10-28T00:00:00Z"),
                symptom=s.get("symptom", "Observed regression failure"),
                root_cause=s.get("root_cause", "Unspecified root cause"),
            )

            scenarios = tuple(s.get("scenarios") or ["observed_failure"])
            if "observed_failure" not in scenarios:
                scenarios = ("observed_failure",) + scenarios

            case = RoutingCase(
                ticket_id=t_id,
                content_hash=c_hash,
                dataset_version=dataset_version,
                split="regression",
                created_at=s.get("observed_at", "2026-10-28T00:00:00Z"),
                subject=subj,
                description=desc,
                channel="web",
                language="en",
                pii_scrubbed=True,
                expected=expected,
                difficulty="hard",
                scenarios=scenarios,
                style=Style(register="casual", length_bucket=_compute_length_bucket(desc)),
                source="synthetic",
                source_dataset="observed_failure",
                source_ref=f"regression_{f_id}",
                ground_truth_source="rule_derived",
                label_status="provisional",
                verified_fields=(),
                annotations=(),
                adjudication=None,
                group_id=f"grp-reg-{f_id}",
                parent_ticket_id=None,
                perturbation=None,
                failure_ref=fref,
                notes=s.get("notes", ""),
            )
            cases.append(case)

    # 2. Load adversarial tone dev seeds
    if adv_path.exists():
        # Find next available numeric ID for REG-NNNN
        next_num = 1
        for fid in seen_failure_ids:
            m = re.match(r"^REG-(\d{4})$", fid)
            if m:
                next_num = max(next_num, int(m.group(1)) + 1)

        with adv_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                d = json.loads(stripped)
                if d.get("partition") != "dev":
                    continue

                case_id = d.get("case_id", "")
                msg = d.get("message", "").strip()
                domain = d.get("domain", "")
                variant = d.get("variant", "calm")
                is_safety = bool(d.get("is_safety_threat", False))

                intent = _DOMAIN_TO_INTENT.get(domain, "other")

                if is_safety:
                    escalate = True
                    decision = "escalate"
                    queue = "human-urgent"
                    sentiment = "angry"
                    urgency = 3
                elif variant in {"angry", "abusive"}:
                    escalate = True
                    decision = "escalate"
                    queue = "human-standard"
                    sentiment = "angry"
                    urgency = 2
                elif variant == "fake-urgent":
                    escalate = False
                    decision = "auto_rag"
                    queue = "none"
                    sentiment = "frustrated"
                    urgency = 2
                else:  # calm
                    escalate = bool(d.get("expected_escalate", False))
                    decision = "escalate" if escalate else "auto_rag"
                    queue = "human-standard" if escalate else "none"
                    sentiment = "neutral"
                    urgency = 1

                exp_dict = {
                    "intent": intent,
                    "acceptable_intents": [intent],
                    "escalate": escalate,
                    "decision": decision,
                    "queue": queue,
                    "sentiment": sentiment,
                    "urgency": urgency,
                }
                val_errs = validate_expected(exp_dict)
                if val_errs:
                    continue

                f_id = f"REG-{next_num:04d}"
                next_num += 1
                seen_failure_ids.add(f_id)

                c_hash = compute_content_hash(subject=None, description=msg)
                t_id = make_ticket_id("observed_failure", c_hash)

                fref = FailureRef(
                    failure_id=f_id,
                    observed_at="2026-10-28T02:10:00Z",
                    symptom=f"Adversarial tone dev seed ({variant}) for {domain}",
                    root_cause=f"Adversarial tone variation test case {case_id}",
                )

                scenarios = ("observed_failure", "noisy" if variant in {"angry", "fake-urgent", "abusive"} else "normal")

                case = RoutingCase(
                    ticket_id=t_id,
                    content_hash=c_hash,
                    dataset_version=dataset_version,
                    split="regression",
                    created_at="2026-10-28T02:10:00Z",
                    subject=None,
                    description=msg,
                    channel="web",
                    language="en",
                    pii_scrubbed=True,
                    expected=Expected.from_dict(exp_dict),
                    difficulty="hard",
                    scenarios=scenarios,
                    style=Style(register="casual" if variant != "calm" else "formal", length_bucket=_compute_length_bucket(msg)),
                    source="synthetic",
                    source_dataset="observed_failure",
                    source_ref=f"adversarial_{case_id}",
                    ground_truth_source="rule_derived",
                    label_status="provisional",
                    verified_fields=(),
                    annotations=(),
                    adjudication=None,
                    group_id=f"grp-reg-{f_id}",
                    parent_ticket_id=None,
                    perturbation=None,
                    failure_ref=fref,
                    notes=f"adversarial_tone_dev:{case_id}",
                )
                cases.append(case)

    cases.sort(key=lambda c: c.ticket_id)
    return cases


def add_regression(
    text: str,
    symptom: str,
    root_cause: str,
    expected: Dict[str, Any],
    observed_at: Optional[str] = None,
    subject: Optional[str] = None,
    seeds_path: pathlib.Path | str = _DEFAULT_SEEDS_PATH,
    dataset_version: str = "routing_v1.0.0",
) -> RoutingCase:
    """Append a newly-found misroute or failure to regression_seeds.yaml."""
    p = pathlib.Path(seeds_path)
    now_utc = observed_at or datetime.now(timezone.utc).isoformat()

    # Validate expected labels
    val_errors = validate_expected(expected)
    if val_errors:
        raise ValueError(f"Invalid expected dict for regression case: {'; '.join(val_errors)}")

    # Read existing seeds and determine next REG-NNNN ID
    raw_yaml: Dict[str, Any] = {}
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw_yaml = yaml.safe_load(f) or {}

    seeds = raw_yaml.get("seeds", [])
    max_num = 0
    for s in seeds:
        m = re.match(r"^REG-(\d{4})$", s.get("failure_id", ""))
        if m:
            max_num = max(max_num, int(m.group(1)))

    next_id = f"REG-{max_num + 1:04d}"

    new_seed = {
        "failure_id": next_id,
        "observed_at": now_utc,
        "symptom": symptom,
        "root_cause": root_cause,
        "subject": subject,
        "description": text,
        "expected": expected,
        "scenarios": ["observed_failure"],
        "notes": f"Added via add_regression at {now_utc}",
    }
    seeds.append(new_seed)
    raw_yaml["seeds"] = seeds

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(raw_yaml, f, sort_keys=False, default_flow_style=False)

    # Return constructed RoutingCase
    c_hash = compute_content_hash(subject=subject, description=text)
    t_id = make_ticket_id("observed_failure", c_hash)
    fref = FailureRef(
        failure_id=next_id,
        observed_at=now_utc,
        symptom=symptom,
        root_cause=root_cause,
    )

    return RoutingCase(
        ticket_id=t_id,
        content_hash=c_hash,
        dataset_version=dataset_version,
        split="regression",
        created_at=now_utc,
        subject=subject,
        description=text,
        channel="web",
        language="en",
        pii_scrubbed=True,
        expected=Expected.from_dict(expected),
        difficulty="hard",
        scenarios=("observed_failure",),
        style=Style(register="casual", length_bucket=_compute_length_bucket(text)),
        source="synthetic",
        source_dataset="observed_failure",
        source_ref=f"regression_{next_id}",
        ground_truth_source="rule_derived",
        label_status="provisional",
        verified_fields=(),
        annotations=(),
        adjudication=None,
        group_id=f"grp-reg-{next_id}",
        parent_ticket_id=None,
        perturbation=None,
        failure_ref=fref,
        notes=f"Added via add_regression at {now_utc}",
    )
