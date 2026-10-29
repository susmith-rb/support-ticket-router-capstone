"""Routing case JSONL schema v1 and serialization helpers.

# REQ-REVAL-002: Routing case JSONL schema v1.
Provides immutable dataclass definitions for routing cases, annotations,
adjudications, and perturbations, with canonical serialization, validation,
and deterministic content hashing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ticket_router.config.taxonomy import validate_expected

SCHEMA_VERSION = "routing-case/v1"

VALID_SPLITS = ("dev", "regression", "heldout", "pool")
VALID_CHANNELS = ("twitter", "email", "web", "chat")
VALID_DIFFICULTIES = ("easy", "medium", "hard")
VALID_SCENARIOS = (
    "normal",
    "ambiguous",
    "missing_info",
    "noisy",
    "long",
    "rare",
    "ood",
    "conflicting",
    "prompt_injection",
    "observed_failure",
    "paraphrase",
    "perturbation",
)
VALID_REGISTERS = ("formal", "casual", "terse", "emotional", "non_native", "sms")
VALID_LENGTH_BUCKETS = ("short (<20 words)", "medium (20–80)", "long (>80)")
VALID_SOURCES = ("real", "public", "synthetic")
VALID_SOURCE_DATASETS = (
    "kaggle_twcs",
    "bitext_cs",
    "synthetic_jetski",
    "synthetic_template",
    "perturbation",
    "observed_failure",
)
VALID_ANNOTATOR_TYPES = ("human", "llm", "teacher", "mapping", "generator")
VALID_VERDICTS = ("accept", "relabel", "reject", "unsure")
VALID_LABEL_STATUSES = ("verified", "provisional", "disputed", "rejected")
VALID_GROUND_TRUTH_SOURCES = (
    "human_adjudicated",
    "human_consensus",
    "human_single",
    "teacher_consensus",
    "mapped_public",
    "llm_provisional",
    "synthetic_intended",
    "rule_derived",
)

# Text normalisation regexes
_RE_MENTION = re.compile(r"@[A-Za-z0-9_]+")
_RE_URL = re.compile(r"https?://\S+|www\.\S+")
_RE_REDACTION = re.compile(r"\[[A-Za-z0-9_]+\]")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_FAILURE_ID = re.compile(r"^REG-\d{4}$")


def normalize_text(text: str) -> str:
    """Normalize text per REQ-REVAL-008: NFKC, lowercase, collapse whitespace, strip @mentions/URLs/redaction tokens."""
    if not text:
        return ""
    # Unicode NFKC normalization
    text = unicodedata.normalize("NFKC", text)
    # Lowercase
    text = text.lower()
    # Strip @mentions
    text = _RE_MENTION.sub("", text)
    # Strip URLs
    text = _RE_URL.sub("", text)
    # Strip redaction tokens e.g. [ORDER], [EMAIL], [PHONE]
    text = _RE_REDACTION.sub("", text)
    # Collapse whitespace and strip leading/trailing
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text


def compute_content_hash(subject: Optional[str], description: str) -> str:
    """Compute sha256 of normalized subject and description."""
    norm_subj = normalize_text(subject or "")
    norm_desc = normalize_text(description or "")
    combined = f"{norm_subj}\n{norm_desc}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def make_ticket_id(source_dataset: str, content_hash: str) -> str:
    """Generate canonical ticket_id format: rt-<source_dataset>-<sha8>."""
    sha8 = content_hash[:8]
    return f"rt-{source_dataset}-{sha8}"


@dataclass(frozen=True)
class Expected:
    """Expected routing labels and behavioural targets."""

    intent: str
    acceptable_intents: Tuple[str, ...]
    escalate: bool
    decision: str
    queue: str
    sentiment: Optional[str] = None
    urgency: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict with stable canonical key order."""
        out: Dict[str, Any] = {
            "intent": self.intent,
            "acceptable_intents": list(self.acceptable_intents),
            "escalate": self.escalate,
            "decision": self.decision,
            "queue": self.queue,
            "sentiment": self.sentiment,
            "urgency": self.urgency,
        }
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Expected:
        return cls(
            intent=d["intent"],
            acceptable_intents=tuple(d.get("acceptable_intents", [d["intent"]])),
            escalate=bool(d["escalate"]),
            decision=d["decision"],
            queue=d["queue"],
            sentiment=d.get("sentiment"),
            urgency=d.get("urgency"),
        )


@dataclass(frozen=True)
class Style:
    """Register and length bucket descriptors for ticket text."""

    register: str
    length_bucket: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "register": self.register,
            "length_bucket": self.length_bucket,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Style:
        return cls(
            register=d["register"],
            length_bucket=d["length_bucket"],
        )


@dataclass(frozen=True)
class Annotation:
    """Single pass annotation from human, LLM, teacher, mapping, or generator."""

    annotator_id: str
    annotator_type: str
    labels: Dict[str, Any]
    confidence: Optional[float] = None
    rationale: str = ""
    pass_index: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "annotator_id": self.annotator_id,
            "annotator_type": self.annotator_type,
            "labels": self.labels,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "pass_index": self.pass_index,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Annotation:
        return cls(
            annotator_id=d["annotator_id"],
            annotator_type=d["annotator_type"],
            labels=dict(d.get("labels", {})),
            confidence=float(d["confidence"]) if d.get("confidence") is not None else None,
            rationale=d.get("rationale", ""),
            pass_index=int(d["pass_index"]) if d.get("pass_index") is not None else None,
            created_at=d.get("created_at"),
        )


@dataclass(frozen=True)
class Adjudication:
    """Human adjudication decision from review workflow."""

    adjudicator: str
    decided_at: str
    verdict: str
    rationale: str
    sheet_revision: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adjudicator": self.adjudicator,
            "decided_at": self.decided_at,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "sheet_revision": self.sheet_revision,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Adjudication:
        return cls(
            adjudicator=d["adjudicator"],
            decided_at=d["decided_at"],
            verdict=d["verdict"],
            rationale=d.get("rationale", ""),
            sheet_revision=d.get("sheet_revision"),
        )


@dataclass(frozen=True)
class Perturbation:
    """Provenance for derived synthetic or adversarial perturbations."""

    type: str
    seed: int
    params: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "seed": self.seed,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Perturbation:
        return cls(
            type=d["type"],
            seed=int(d["seed"]),
            params=dict(d.get("params", {})),
        )


@dataclass(frozen=True)
class FailureRef:
    """Reference tracking for regression cases of observed production failures."""

    failure_id: str
    observed_at: str
    symptom: str
    root_cause: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "observed_at": self.observed_at,
            "symptom": self.symptom,
            "root_cause": self.root_cause,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> FailureRef:
        return cls(
            failure_id=d["failure_id"],
            observed_at=d["observed_at"],
            symptom=d["symptom"],
            root_cause=d["root_cause"],
        )


@dataclass(frozen=True)
class RoutingCase:
    """Complete immutable routing evaluation case adhering to routing-case/v1."""

    ticket_id: str
    content_hash: str
    dataset_version: str
    split: str
    created_at: str
    subject: Optional[str]
    description: str
    channel: str
    language: str
    pii_scrubbed: bool
    expected: Expected
    difficulty: str
    scenarios: Tuple[str, ...]
    style: Style
    source: str
    source_dataset: str
    source_ref: str
    ground_truth_source: str
    label_status: str
    verified_fields: Tuple[str, ...]
    annotations: Tuple[Annotation, ...]
    adjudication: Optional[Adjudication]
    group_id: str
    parent_ticket_id: Optional[str]
    perturbation: Optional[Perturbation]
    failure_ref: Optional[FailureRef]
    notes: str = ""
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """Convert RoutingCase to dictionary in canonical key order."""
        return {
            "schema_version": self.schema_version,
            "ticket_id": self.ticket_id,
            "content_hash": self.content_hash,
            "dataset_version": self.dataset_version,
            "split": self.split,
            "created_at": self.created_at,
            "subject": self.subject,
            "description": self.description,
            "channel": self.channel,
            "language": self.language,
            "pii_scrubbed": self.pii_scrubbed,
            "expected": self.expected.to_dict(),
            "difficulty": self.difficulty,
            "scenarios": list(self.scenarios),
            "style": self.style.to_dict(),
            "source": self.source,
            "source_dataset": self.source_dataset,
            "source_ref": self.source_ref,
            "ground_truth_source": self.ground_truth_source,
            "label_status": self.label_status,
            "verified_fields": list(self.verified_fields),
            "annotations": [ann.to_dict() for ann in self.annotations],
            "adjudication": self.adjudication.to_dict() if self.adjudication else None,
            "group_id": self.group_id,
            "parent_ticket_id": self.parent_ticket_id,
            "perturbation": self.perturbation.to_dict() if self.perturbation else None,
            "failure_ref": self.failure_ref.to_dict() if self.failure_ref else None,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RoutingCase:
        """Parse dictionary into immutable RoutingCase instance."""
        exp_raw = d["expected"]
        expected = exp_raw if isinstance(exp_raw, Expected) else Expected.from_dict(exp_raw)

        style_raw = d["style"]
        style = style_raw if isinstance(style_raw, Style) else Style.from_dict(style_raw)

        raw_anns = d.get("annotations", [])
        annotations = tuple(
            ann if isinstance(ann, Annotation) else Annotation.from_dict(ann)
            for ann in raw_anns
        )

        adj_raw = d.get("adjudication")
        adjudication = None
        if adj_raw is not None:
            adjudication = (
                adj_raw if isinstance(adj_raw, Adjudication) else Adjudication.from_dict(adj_raw)
            )

        pert_raw = d.get("perturbation")
        perturbation = None
        if pert_raw is not None:
            perturbation = (
                pert_raw if isinstance(pert_raw, Perturbation) else Perturbation.from_dict(pert_raw)
            )

        fail_raw = d.get("failure_ref")
        failure_ref = None
        if fail_raw is not None:
            failure_ref = (
                fail_raw if isinstance(fail_raw, FailureRef) else FailureRef.from_dict(fail_raw)
            )

        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            ticket_id=d["ticket_id"],
            content_hash=d["content_hash"],
            dataset_version=d["dataset_version"],
            split=d["split"],
            created_at=d["created_at"],
            subject=d.get("subject"),
            description=d["description"],
            channel=d["channel"],
            language=d["language"],
            pii_scrubbed=bool(d["pii_scrubbed"]),
            expected=expected,
            difficulty=d["difficulty"],
            scenarios=tuple(d.get("scenarios", [])),
            style=style,
            source=d["source"],
            source_dataset=d["source_dataset"],
            source_ref=d["source_ref"],
            ground_truth_source=d["ground_truth_source"],
            label_status=d["label_status"],
            verified_fields=tuple(d.get("verified_fields", [])),
            annotations=annotations,
            adjudication=adjudication,
            group_id=d.get("group_id", ""),
            parent_ticket_id=d.get("parent_ticket_id"),
            perturbation=perturbation,
            failure_ref=failure_ref,
            notes=d.get("notes", ""),
        )


def validate_case(d: Dict[str, Any] | RoutingCase) -> List[str]:
    """Validate a case dictionary or instance against schema v1 rules.

    Returns:
        List of error strings. Empty list indicates full compliance.
    """
    if isinstance(d, RoutingCase):
        raw = d.to_dict()
    elif isinstance(d, dict):
        raw = d
    else:
        return [f"Input must be a dict or RoutingCase, got {type(d).__name__}"]

    errors: List[str] = []

    # 1. Schema version
    if raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"Invalid schema_version '{raw.get('schema_version')}'; expected '{SCHEMA_VERSION}'"
        )

    # 2. Identity fields
    ticket_id = raw.get("ticket_id")
    source_dataset = raw.get("source_dataset")
    content_hash = raw.get("content_hash")

    if not ticket_id or not isinstance(ticket_id, str):
        errors.append("Field 'ticket_id' must be a non-empty string")
    elif source_dataset and content_hash and isinstance(source_dataset, str) and isinstance(content_hash, str):
        expected_prefix = f"rt-{source_dataset}-"
        if not ticket_id.startswith(expected_prefix) or len(ticket_id) != len(expected_prefix) + 8:
            errors.append(
                f"ticket_id '{ticket_id}' does not match expected format 'rt-{source_dataset}-<sha8>'"
            )

    if not content_hash or not isinstance(content_hash, str) or len(content_hash) != 64:
        errors.append("Field 'content_hash' must be a valid 64-char sha256 hex string")

    if not raw.get("dataset_version"):
        errors.append("Field 'dataset_version' is required")

    split = raw.get("split")
    if split not in VALID_SPLITS:
        errors.append(f"Invalid split '{split}'. Must be in {list(VALID_SPLITS)}")

    if not raw.get("created_at"):
        errors.append("Field 'created_at' is required")

    # 3. Content fields
    description = raw.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        errors.append("Field 'description' must be a non-empty string")

    channel = raw.get("channel")
    if channel not in VALID_CHANNELS:
        errors.append(f"Invalid channel '{channel}'. Must be in {list(VALID_CHANNELS)}")

    language = raw.get("language")
    scenarios = raw.get("scenarios") or []
    split = raw.get("split")
    if language != "en":
        if split != "pool":
            errors.append(
                f"Non-English language '{language}' is only allowed in 'pool' split (got split='{split}')"
            )
        if "ood" not in scenarios and "out_of_scope_language" not in (raw.get("notes") or ""):
            errors.append(
                f"Non-English language '{language}' is only allowed when scenarios include 'ood' or notes tag 'out_of_scope_language'"
            )

    if raw.get("pii_scrubbed") is not True:
        errors.append("Field 'pii_scrubbed' must be True")

    # 4. Expected behaviour
    expected = raw.get("expected")
    if not expected:
        errors.append("Field 'expected' is required")
    else:
        exp_dict = expected.to_dict() if hasattr(expected, "to_dict") else expected
        exp_errors = validate_expected(exp_dict)
        errors.extend(f"expected: {e}" for e in exp_errors)

    # 5. Coverage fields
    difficulty = raw.get("difficulty")
    if difficulty not in VALID_DIFFICULTIES:
        errors.append(
            f"Invalid difficulty '{difficulty}'. Must be in {list(VALID_DIFFICULTIES)}"
        )

    if not scenarios or not isinstance(scenarios, (list, tuple)):
        errors.append("Field 'scenarios' must be a non-empty list or tuple")
    else:
        for sc in scenarios:
            if sc not in VALID_SCENARIOS:
                errors.append(
                    f"Invalid scenario '{sc}'. Must be one of {list(VALID_SCENARIOS)}"
                )

    style = raw.get("style")
    if not style:
        errors.append("Field 'style' is required")
    else:
        style_dict = style.to_dict() if hasattr(style, "to_dict") else style
        reg = style_dict.get("register")
        if reg not in VALID_REGISTERS:
            errors.append(
                f"Invalid style register '{reg}'. Must be in {list(VALID_REGISTERS)}"
            )
        lb = style_dict.get("length_bucket")
        if lb not in VALID_LENGTH_BUCKETS:
            errors.append(
                f"Invalid style length_bucket '{lb}'. Must be in {list(VALID_LENGTH_BUCKETS)}"
            )

    # 6. Provenance fields
    source = raw.get("source")
    if source not in VALID_SOURCES:
        errors.append(f"Invalid source '{source}'. Must be in {list(VALID_SOURCES)}")

    if source_dataset not in VALID_SOURCE_DATASETS:
        errors.append(
            f"Invalid source_dataset '{source_dataset}'. Must be in {list(VALID_SOURCE_DATASETS)}"
        )

    gt_source = raw.get("ground_truth_source")
    if gt_source not in VALID_GROUND_TRUTH_SOURCES:
        errors.append(
            f"Invalid ground_truth_source '{gt_source}'. Must be in {list(VALID_GROUND_TRUTH_SOURCES)}"
        )

    label_status = raw.get("label_status")
    if label_status not in VALID_LABEL_STATUSES:
        errors.append(
            f"Invalid label_status '{label_status}'. Must be in {list(VALID_LABEL_STATUSES)}"
        )

    # 7. Adjudication validation
    adj = raw.get("adjudication")
    if adj is not None:
        adj_dict = adj.to_dict() if hasattr(adj, "to_dict") else adj
        v = adj_dict.get("verdict")
        if v not in VALID_VERDICTS:
            errors.append(
                f"Invalid adjudication verdict '{v}'. Must be in {list(VALID_VERDICTS)}"
            )
        if not adj_dict.get("adjudicator"):
            errors.append("Adjudication requires 'adjudicator'")

    # 8. Failure reference validation
    fref = raw.get("failure_ref")
    if fref is not None:
        fref_dict = fref.to_dict() if hasattr(fref, "to_dict") else fref
        fid = fref_dict.get("failure_id", "")
        if not _RE_FAILURE_ID.match(fid):
            errors.append(
                f"failure_ref.failure_id '{fid}' must match pattern REG-NNNN"
            )

    return errors


def read_jsonl(path: str | Path) -> List[RoutingCase]:
    """Read a JSONL file into a list of RoutingCase instances."""
    p = Path(path)
    cases: List[RoutingCase] = []
    with p.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
                cases.append(RoutingCase.from_dict(data))
            except Exception as e:
                raise ValueError(f"Error parsing JSON on line {line_no} of {p}: {e}") from e
    return cases


def write_jsonl(path: str | Path, cases: Iterable[RoutingCase]) -> None:
    """Write an iterable of RoutingCase instances to a deterministic JSONL file.

    Cases are written sorted by ticket_id with UTF-8 encoding and newline endings.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sorted_cases = sorted(cases, key=lambda c: c.ticket_id)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        for case in sorted_cases:
            line = json.dumps(case.to_dict(), ensure_ascii=False)
            f.write(f"{line}\n")
