"""Human adjudication workflow using Google Sheets TSV export/ingest.

# REQ-REVAL-007: Human adjudication workflow (Google Sheet)
Provides:
1. export_queue(cases, out_tsv) exporting queued cases with candidate labels and empty decision columns.
2. ingest_decisions(tsv_path, cases, supersede) validating decisions, enforcing audit rationales,
   deriving human_adjudicated / human_consensus, ensuring idempotence, and appending to adjudication_log.jsonl.
"""

from __future__ import annotations

import csv
import json
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from evals.routing.provenance import derive_status
from evals.routing.schema import (
    Adjudication,
    Annotation,
    Expected,
    RoutingCase,
)
from ticket_router.config.taxonomy import validate_expected

TSV_COLUMNS: List[str] = [
    "ticket_id",
    "description",
    "subject",
    "candidate_intent",
    "candidate_decision",
    "candidate_queue",
    "candidate_sources",
    "reason_queued",
    "final_intent",
    "final_acceptable_intents",
    "final_escalate",
    "final_decision",
    "final_queue",
    "final_sentiment",
    "final_urgency",
    "verdict",
    "adjudicator",
    "rationale",
]

VALID_VERDICTS: Tuple[str, ...] = ("accept", "relabel", "reject", "unsure")


def export_queue(
    items: Sequence[Union[Tuple[RoutingCase, str], RoutingCase]],
    out_tsv: pathlib.Path | str,
) -> int:
    """Export queued adjudication cases to TSV for Google Sheets review."""
    p = pathlib.Path(out_tsv)
    p.parent.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []

    for item in items:
        if isinstance(item, tuple):
            case, reason = item
        else:
            case = item
            reason = "manual_adjudication_request"

        # Extract candidate sources
        cand_sources = [a.annotator_type for a in case.annotations]
        cand_sources_str = ", ".join(sorted(set(cand_sources))) if cand_sources else case.source_dataset

        row = {
            "ticket_id": case.ticket_id,
            "description": case.description,
            "subject": case.subject or "",
            "candidate_intent": case.expected.intent,
            "candidate_decision": case.expected.decision,
            "candidate_queue": case.expected.queue,
            "candidate_sources": cand_sources_str,
            "reason_queued": reason,
            "final_intent": "",
            "final_acceptable_intents": "",
            "final_escalate": "",
            "final_decision": "",
            "final_queue": "",
            "final_sentiment": "",
            "final_urgency": "",
            "verdict": "",
            "adjudicator": "",
            "rationale": "",
        }
        rows.append(row)

    rows.sort(key=lambda r: r["ticket_id"])

    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def ingest_decisions(
    tsv_path: pathlib.Path | str,
    cases: Sequence[RoutingCase],
    supersede: bool = False,
    log_path: pathlib.Path | str = "evals/data/routing_v1/adjudication_log.jsonl",
    sheet_revision: Optional[str] = None,
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Ingest decisions from an adjudication TSV, validate, and update cases.

    Enforces:
    - Verdict in accept, relabel, reject, unsure.
    - Relabel and reject require non-empty adjudicator and rationale.
    - Decision labels validate against taxonomy rules.
    - Idempotent: identical re-import is a no-op; conflicting re-decision raises unless supersede=True.
    - Two agreeing independent adjudicators promote ground_truth_source to human_consensus.
    - Appends decision records to adjudication_log.jsonl.
    """
    p = pathlib.Path(tsv_path)
    if not p.exists():
        raise FileNotFoundError(f"Adjudication TSV not found at: {p}")

    cases_by_id = {c.ticket_id: c for c in cases}
    updated_cases: Dict[str, RoutingCase] = dict(cases_by_id)
    now_utc = datetime.now(timezone.utc).isoformat()

    log_entries: List[Dict[str, Any]] = []
    processed_count = 0

    with p.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for line_no, row in enumerate(reader, 2):
            t_id = row.get("ticket_id", "").strip()
            verdict = row.get("verdict", "").strip().lower()
            if not t_id or not verdict:
                continue

            if t_id not in cases_by_id:
                raise KeyError(f"Ticket ID '{t_id}' on row {line_no} does not exist in cases")

            if verdict not in VALID_VERDICTS:
                raise ValueError(
                    f"Invalid verdict '{verdict}' on row {line_no}. Must be in {list(VALID_VERDICTS)}"
                )

            adjudicator = row.get("adjudicator", "").strip()
            rationale = row.get("rationale", "").strip()

            if verdict in {"relabel", "reject"}:
                if not adjudicator:
                    raise ValueError(f"Verdict '{verdict}' on row {line_no} requires 'adjudicator'")
                if not rationale:
                    raise ValueError(f"Verdict '{verdict}' on row {line_no} requires non-empty 'rationale'")

            case = updated_cases[t_id]
            existing_adj = case.adjudication

            # Determine decided labels
            if verdict == "accept":
                final_expected = case.expected
                verified_fields = ("intent", "decision", "queue", "escalate")
                gt_source = "human_adjudicated"
            elif verdict == "relabel":
                f_intent = row.get("final_intent", "").strip() or case.expected.intent
                acc_raw = row.get("final_acceptable_intents", "").strip()
                if acc_raw:
                    f_acceptable = tuple(i.strip() for i in acc_raw.split(",") if i.strip())
                else:
                    f_acceptable = (f_intent,)

                f_decision = row.get("final_decision", "").strip() or case.expected.decision
                f_queue = row.get("final_queue", "").strip() or case.expected.queue
                esc_raw = row.get("final_escalate", "").strip().lower()
                f_escalate = esc_raw in {"true", "1", "yes"} if esc_raw else (f_decision == "escalate")

                exp_dict = {
                    "intent": f_intent,
                    "acceptable_intents": list(f_acceptable),
                    "escalate": f_escalate,
                    "decision": f_decision,
                    "queue": f_queue,
                    "sentiment": row.get("final_sentiment", "").strip() or case.expected.sentiment,
                    "urgency": int(row["final_urgency"].strip()) if row.get("final_urgency", "").strip() else case.expected.urgency,
                }
                val_errors = validate_expected(exp_dict)
                if val_errors:
                    raise ValueError(
                        f"Validation error in relabeled expected for ticket '{t_id}': {'; '.join(val_errors)}"
                    )

                final_expected = Expected.from_dict(exp_dict)
                verified_fields = ("intent", "decision", "queue", "escalate")
                gt_source = "human_adjudicated"
            elif verdict == "reject":
                final_expected = case.expected
                verified_fields = ()
                gt_source = case.ground_truth_source
            else:  # unsure
                final_expected = case.expected
                verified_fields = ()
                gt_source = case.ground_truth_source

            # Idempotency and conflict checks
            if existing_adj is not None:
                # Idempotency check: if identical decision already recorded by same adjudicator
                if (
                    existing_adj.verdict == verdict
                    and existing_adj.adjudicator == adjudicator
                    and existing_adj.rationale == rationale
                ):
                    continue

                is_conflict = False
                if existing_adj.verdict != verdict:
                    is_conflict = True
                elif verdict == "relabel" and case.expected != final_expected:
                    is_conflict = True

                if is_conflict and not supersede:
                    raise ValueError(
                        f"Conflicting adjudication for ticket '{t_id}' (existing verdict '{existing_adj.verdict}' "
                        f"by '{existing_adj.adjudicator}'; new verdict '{verdict}' by '{adjudicator}'). "
                        "Pass supersede=True to overwrite."
                    )

                # Two distinct agreeing independent adjudicators yield human_consensus
                if (
                    not is_conflict
                    and existing_adj.adjudicator != adjudicator
                    and bool(existing_adj.adjudicator)
                    and bool(adjudicator)
                    and verdict in {"accept", "relabel"}
                ):
                    gt_source = "human_consensus"

            new_adj = Adjudication(
                adjudicator=adjudicator or "adjudicator",
                decided_at=now_utc,
                verdict=verdict,
                rationale=rationale,
                sheet_revision=sheet_revision,
            )

            ann = Annotation(
                annotator_id=f"adjudicator_{adjudicator}",
                annotator_type="human",
                labels=final_expected.to_dict(),
                confidence=1.0,
                rationale=rationale,
                pass_index=0,
                created_at=now_utc,
            )

            # Create updated case
            raw_case_dict = case.to_dict()
            raw_case_dict["expected"] = final_expected.to_dict()
            raw_case_dict["adjudication"] = new_adj.to_dict()
            raw_case_dict["ground_truth_source"] = gt_source
            raw_case_dict["verified_fields"] = list(verified_fields)
            raw_case_dict["annotations"].append(ann.to_dict())

            # Derive label_status canonically
            if verdict == "unsure":
                raw_case_dict["label_status"] = "disputed"
            else:
                raw_case_dict["label_status"] = derive_status(raw_case_dict)

            updated_cases[t_id] = RoutingCase.from_dict(raw_case_dict)
            processed_count += 1

            log_entries.append({
                "ticket_id": t_id,
                "adjudicator": adjudicator,
                "verdict": verdict,
                "decided_at": now_utc,
                "rationale": rationale,
                "sheet_revision": sheet_revision,
                "expected": final_expected.to_dict(),
            })

    # Append audit records to adjudication_log.jsonl
    if log_entries:
        lp = pathlib.Path(log_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8", newline="\n") as f:
            for entry in log_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    res_list = list(updated_cases.values())
    res_list.sort(key=lambda c: c.ticket_id)

    report = {
        "processed_count": processed_count,
        "log_path": str(log_path),
    }
    return res_list, report
