"""Real dataset source adapter for Kaggle TWCS (Customer Support on Twitter).

# REQ-REVAL-004: Source adapters: real TWCS
Provides:
1. Production-grade PII scrubber removing order IDs, card numbers, emails, URLs, phones, signatures.
2. Contamination guard against Laya training/validation split content hashes.
3. Loader for retrieval_v1 human-adjudicated C2 labels (rag/labels/retrieval_v1/labels_C2.jsonl).
4. Loader for router golden set cross-family teacher consensus (training/router_s1/results/v2/golden_all_model_outputs.csv).
5. Optional lazy-loaded parquet sampler for fresh first-turn tweets.
"""

from __future__ import annotations

import collections
import csv
import json
import pathlib
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from evals.routing.schema import (
    Annotation,
    Expected,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
)

_ORDER_REGEX = re.compile(
    r"(?i)\b(?:order|ord)\.?\s*(?:no\.?|number|#|id)?\s*[:\-#]?\s*(\d{3}-\d{7}-\d{7}|\d{5,12})\b|\b\d{3}-\d{7}-\d{7}\b"
)
_CARD_REGEX = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_PHONE_REGEX = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_SIG_REGEX = re.compile(r"\s*[\^\-–—]\s?[A-Z]{1,3}\b\s*$")
_EMAIL_REGEX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_REGEX = re.compile(r"https?://\S+|www\.\S+")
_USER_MENTION_REGEX = re.compile(r"@\d+")
_HANDLE_MENTION_REGEX = re.compile(r"@[A-Za-z0-9_]+")


def luhn_ok(digits: str) -> bool:
    """Validate Luhn algorithm for credit card detection."""
    d = [int(c) for c in digits][::-1]
    if len(d) < 13:
        return False
    total = sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(d))
    return total % 10 == 0


def scrub_text(text: str) -> str:
    """Scrub PII from real ticket text, preserving order references before card scrubbing."""
    if not text:
        return ""
    t = str(text)
    # Remove customer service agent signoffs like ^AB or -JL
    t = _SIG_REGEX.sub("", t)
    # Replace email addresses and URLs
    t = _EMAIL_REGEX.sub("[EMAIL]", t)
    t = _URL_REGEX.sub("[URL]", t)
    # Standardize Twitter pseudonym handles
    t = _USER_MENTION_REGEX.sub("@user", t)
    t = _HANDLE_MENTION_REGEX.sub("@user", t)
    # Preserve order numbers before card regex
    t = _ORDER_REGEX.sub(
        lambda m: "order [ORDER]" if m.group(0).lower().startswith(("order", "ord")) else "[ORDER]",
        t,
    )
    # Scrub credit cards if Luhn check passes
    t = _CARD_REGEX.sub(
        lambda m: "[CARD]" if luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0),
        t,
    )
    # Scrub phone numbers (>= 10 digits)
    t = _PHONE_REGEX.sub(
        lambda m: "[PHONE]" if len(re.sub(r"\D", "", m.group(0))) >= 10 else m.group(0),
        t,
    )
    # Normalize whitespaces
    return re.sub(r"\s+", " ", t).strip()


def contamination_guard(
    cases: Sequence[RoutingCase],
    train_hashes: Set[str],
) -> Tuple[List[RoutingCase], Dict[str, Any]]:
    """Check cases against Laya train/val corpus content hashes.

    Flags colliding cases with note 'laya_train_collision' and returns a detailed report.
    Colliding cases MUST NOT be assigned to heldout.
    """
    checked = 0
    collisions: List[str] = []
    guarded_cases: List[RoutingCase] = []

    for c in cases:
        checked += 1
        # Recompute canonical hash of normalized text
        norm_hash = compute_content_hash(c.subject, c.description)
        is_collision = (norm_hash in train_hashes) or (c.content_hash in train_hashes)

        if is_collision:
            collisions.append(c.ticket_id)
            collision_note = "laya_train_collision"
            existing_notes = c.notes or ""
            new_notes = f"{existing_notes}; {collision_note}".strip("; ")
            # Colliding cases must never be assigned to heldout (force split=dev)
            new_split = "dev" if c.split == "heldout" else c.split
            # Return new case with updated notes
            guarded_cases.append(
                RoutingCase(
                    ticket_id=c.ticket_id,
                    content_hash=c.content_hash,
                    dataset_version=c.dataset_version,
                    split=new_split,
                    created_at=c.created_at,
                    subject=c.subject,
                    description=c.description,
                    channel=c.channel,
                    language=c.language,
                    pii_scrubbed=c.pii_scrubbed,
                    expected=c.expected,
                    difficulty=c.difficulty,
                    scenarios=c.scenarios,
                    style=c.style,
                    source=c.source,
                    source_dataset=c.source_dataset,
                    source_ref=c.source_ref,
                    ground_truth_source=c.ground_truth_source,
                    label_status=c.label_status,
                    verified_fields=c.verified_fields,
                    annotations=c.annotations,
                    adjudication=c.adjudication,
                    group_id=c.group_id,
                    parent_ticket_id=c.parent_ticket_id,
                    perturbation=c.perturbation,
                    failure_ref=c.failure_ref,
                    notes=new_notes,
                )
            )
        else:
            guarded_cases.append(c)

    report = {
        "checked": checked,
        "collisions_count": len(collisions),
        "colliding_ticket_ids": collisions,
    }
    return guarded_cases, report


def _compute_length_bucket(text: str) -> str:
    count = len(text.split())
    if count < 20:
        return "short (<20 words)"
    if count <= 80:
        return "medium (20–80)"
    return "long (>80)"


def load_retrieval_c2(
    labels_path: pathlib.Path | str,
    queries_path: pathlib.Path | str,
    manifest_path: pathlib.Path | str,
    train_hashes: Optional[Set[str]] = None,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Load human-adjudicated retrieval C2 cases.

    Maps primary FAQ article to canonical intent family via manifest.jsonl.
    Ground truth source is human_adjudicated with verified_fields=['intent'].
    Expected decision/queue is provisional from rule 'auto_rag unless intent other'.
    """
    lbl_p = pathlib.Path(labels_path)
    qry_p = pathlib.Path(queries_path)
    mnf_p = pathlib.Path(manifest_path)

    # 1. Load article id -> intent_family
    article_to_intent: Dict[str, str] = {}
    with mnf_p.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            entry = json.loads(stripped)
            art_id = entry.get("id")
            intent_fam = entry.get("structData", {}).get("intent_family", "other")
            if art_id:
                article_to_intent[art_id] = intent_fam

    # 2. Load qid -> query
    qid_to_query: Dict[str, str] = {}
    with qry_p.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            entry = json.loads(stripped)
            qid_to_query[entry["qid"]] = entry.get("query", "")

    # 3. Load C2 labels
    cases: List[RoutingCase] = []
    with lbl_p.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            c2 = json.loads(stripped)
            qid = c2["qid"]
            raw_query = qid_to_query.get(qid, "")
            if not raw_query.strip():
                continue

            scrubbed_query = scrub_text(raw_query)
            primary_article = c2.get("primary")
            note = c2.get("note", "")

            if primary_article and primary_article in article_to_intent:
                intent = article_to_intent[primary_article]
                gt_source = "human_adjudicated"
                label_status = "verified"
                verified_fields: Tuple[str, ...] = ("intent",)
                ann_type = "human"
            else:
                intent = "other"
                gt_source = "rule_derived"
                label_status = "provisional"
                verified_fields = ()
                ann_type = "mapping"

            # Derive provisional decision/queue per rule 'auto_rag unless intent other'
            if intent == "other":
                decision = "escalate"
                queue = "human-standard"
                escalate = True
            else:
                decision = "auto_rag"
                queue = "none"
                escalate = False

            expected = Expected(
                intent=intent,
                acceptable_intents=(intent,),
                escalate=escalate,
                decision=decision,
                queue=queue,
                sentiment="neutral",
                urgency=1,
            )

            c_hash = compute_content_hash(subject=None, description=scrubbed_query)
            t_id = make_ticket_id("kaggle_twcs", c_hash)

            annotation = Annotation(
                annotator_id="retrieval_v1_C2",
                annotator_type=ann_type,
                labels={"intent": intent},
                confidence=1.0 if verified_fields else 0.8,
                rationale=note or f"Mapped from C2 primary article: {primary_article}",
                pass_index=0,
                created_at="2026-10-24T07:50:00Z",
            )

            style = Style(
                register="casual",
                length_bucket=_compute_length_bucket(scrubbed_query),
            )

            case = RoutingCase(
                ticket_id=t_id,
                content_hash=c_hash,
                dataset_version=dataset_version,
                split="pool",
                created_at="2026-10-24T07:50:00Z",
                subject=None,
                description=scrubbed_query,
                channel="twitter",
                language=c2.get("language", "en"),
                pii_scrubbed=True,
                expected=expected,
                difficulty="medium" if intent != "other" else "hard",
                scenarios=("normal",),
                style=style,
                source="real",
                source_dataset="kaggle_twcs",
                source_ref=f"retrieval_c2_{qid}",
                ground_truth_source=gt_source,
                label_status=label_status,
                verified_fields=verified_fields,
                annotations=(annotation,),
                adjudication=None,
                group_id=f"grp-{t_id}",
                parent_ticket_id=None,
                perturbation=None,
                failure_ref=None,
                notes=f"c2_qid:{qid}",
            )
            cases.append(case)

    if train_hashes:
        cases, _ = contamination_guard(cases, train_hashes)

    cases.sort(key=lambda c: c.ticket_id)
    return cases


def _parse_urgency_str(u_str: str) -> int:
    """Extract integer urgency from strings like '2 - within hours' or '1'."""
    match = re.search(r"\d+", str(u_str))
    if match:
        val = int(match.group(0))
        if 0 <= val <= 3:
            return val
    return 1


def load_golden_consensus(
    golden_csv_path: pathlib.Path | str,
    train_hashes: Optional[Set[str]] = None,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Load golden set model outputs and compute cross-model teacher consensus.

    Ground truth source is teacher_consensus (provisional).
    Carries escalate, sentiment, and urgency from teacher consensus.
    """
    path = pathlib.Path(golden_csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Golden CSV not found at: {path}")

    cases: List[RoutingCase] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            raw_message = row.get("message", "").strip()
            if not raw_message:
                continue

            scrubbed_message = scrub_text(raw_message)

            # Extract teacher votes from Gemini, Sonnet, Opus
            intents = [
                row.get("Gemini intent", "").strip(),
                row.get("Sonnet intent", "").strip(),
                row.get("Opus intent", "").strip(),
            ]
            valid_intents = [i for i in intents if i in {"order_status", "shipping_policy", "returns_refunds", "account_payment", "other"}]
            consensus_intent = collections.Counter(valid_intents).most_common(1)[0][0] if valid_intents else "other"

            sentiments = [
                row.get("Gemini sentiment", "").strip(),
                row.get("Sonnet sentiment", "").strip(),
                row.get("Opus sentiment", "").strip(),
            ]
            valid_sentiments = [s for s in sentiments if s in {"angry", "frustrated", "neutral", "positive"}]
            consensus_sentiment = collections.Counter(valid_sentiments).most_common(1)[0][0] if valid_sentiments else "neutral"

            urgencies = [
                _parse_urgency_str(row.get("Gemini urgency", "")),
                _parse_urgency_str(row.get("Sonnet urgency", "")),
                _parse_urgency_str(row.get("Opus urgency", "")),
            ]
            consensus_urgency = collections.Counter(urgencies).most_common(1)[0][0]

            escalate_votes = [
                row.get("Gemini escalate", "").lower() in {"yes", "true", "1"},
                row.get("Sonnet escalate", "").lower() in {"yes", "true", "1"},
                row.get("Opus escalate", "").lower() in {"yes", "true", "1"},
            ]
            consensus_escalate = (sum(escalate_votes) >= 2)
            decision = "escalate" if consensus_escalate else "auto_rag"
            queue = "human-standard" if consensus_escalate else "none"

            expected = Expected(
                intent=consensus_intent,
                acceptable_intents=(consensus_intent,),
                escalate=consensus_escalate,
                decision=decision,
                queue=queue,
                sentiment=consensus_sentiment,
                urgency=consensus_urgency,
            )

            c_hash = compute_content_hash(subject=None, description=scrubbed_message)
            case_id = row.get("case_id", f"G{idx:03d}")
            t_id = make_ticket_id("kaggle_twcs", c_hash)

            annotation = Annotation(
                annotator_id="golden_teacher_consensus",
                annotator_type="teacher",
                labels=expected.to_dict(),
                confidence=0.9,
                rationale=f"Teacher cross-model consensus: Gemini={intents[0]}, Sonnet={intents[1]}, Opus={intents[2]}",
                pass_index=0,
                created_at="2026-10-22T02:26:00Z",
            )

            case = RoutingCase(
                ticket_id=t_id,
                content_hash=c_hash,
                dataset_version=dataset_version,
                split="pool",
                created_at="2026-10-22T02:26:00Z",
                subject=None,
                description=scrubbed_message,
                channel="twitter",
                language="en",
                pii_scrubbed=True,
                expected=expected,
                difficulty="medium",
                scenarios=("normal",),
                style=Style(register="casual", length_bucket=_compute_length_bucket(scrubbed_message)),
                source="real",
                source_dataset="kaggle_twcs",
                source_ref=f"golden_{case_id}",
                ground_truth_source="teacher_consensus",
                label_status="provisional",
                verified_fields=(),
                annotations=(annotation,),
                adjudication=None,
                group_id=f"grp-{t_id}",
                parent_ticket_id=None,
                perturbation=None,
                failure_ref=None,
                notes=f"golden_case_id:{case_id}",
            )
            cases.append(case)

    if train_hashes:
        cases, _ = contamination_guard(cases, train_hashes)

    cases.sort(key=lambda c: c.ticket_id)
    return cases


def sample_inbound_parquet(
    parquet_path: pathlib.Path | str,
    n: int,
    seed: int = 20261028,
) -> List[RoutingCase]:
    """Sample inbound first-turn messages from parquet.

    Requires pandas and pyarrow. Lazily imported so CI can skip if not installed.
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError("pandas is required for sample_inbound_parquet") from e

    path = pathlib.Path(parquet_path)
    if not path.exists():
        raise FileNotFoundError(f"Parquet file not found at: {path}")

    df = pd.read_parquet(path)
    sample_df = df.sample(n=min(n, len(df)), random_state=seed)

    cases: List[RoutingCase] = []
    for idx, row in sample_df.iterrows():
        raw_text = str(row.get("text", row.get("message", "")))
        if not raw_text.strip():
            continue
        scrubbed = scrub_text(raw_text)
        c_hash = compute_content_hash(subject=None, description=scrubbed)
        t_id = make_ticket_id("kaggle_twcs", c_hash)

        expected = Expected(
            intent="other",
            acceptable_intents=("other",),
            escalate=False,
            decision="auto_rag",
            queue="none",
            sentiment="neutral",
            urgency=1,
        )

        cases.append(
            RoutingCase(
                ticket_id=t_id,
                content_hash=c_hash,
                dataset_version="routing_v1.0.0",
                split="pool",
                created_at="2026-10-28T00:00:00Z",
                subject=None,
                description=scrubbed,
                channel="twitter",
                language="en",
                pii_scrubbed=True,
                expected=expected,
                difficulty="medium",
                scenarios=("normal",),
                style=Style(register="casual", length_bucket=_compute_length_bucket(scrubbed)),
                source="real",
                source_dataset="kaggle_twcs",
                source_ref=f"parquet_row_{idx}",
                ground_truth_source="rule_derived",
                label_status="provisional",
                verified_fields=(),
                annotations=(),
                adjudication=None,
                group_id=f"grp-{t_id}",
                parent_ticket_id=None,
                perturbation=None,
                failure_ref=None,
                notes="inbound_first_turn_parquet",
            )
        )

    cases.sort(key=lambda c: c.ticket_id)
    return cases


# Alias for PII scrubber
scrub_pii = scrub_text

_NON_EN_CHARS = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u0600-\u06ff\u0400-\u04ff\u0590-\u05ff]")
_NON_EN_WORDS = {
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "le", "la", "les", "du", "des",
    "est", "sont", "pas", "pour", "avec", "dans", "sur", "mais", "que", "qui", "mon", "ma", "mes",
    "el", "los", "las", "un", "una", "unos", "unas", "del", "al", "es", "son", "por", "para",
    "con", "sin", "sobre", "pero", "como", "mas", "más", "este", "esta", "estos", "estas", "mi", "mis",
    "und", "der", "die", "das", "ein", "eine", "einer", "eines", "nicht", "ist", "sind", "mit", "zu",
    "auf", "für", "von", "den", "dem", "sich", "aber", "auch", "nach", "aus", "wenn", "oder", "bei",
}
_EN_COMMON = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "into", "your", "good", "some", "could", "them", "see", "other",
    "than", "then", "now", "look", "only", "come", "its", "over", "think",
    "also", "back", "after", "use", "two", "how", "our", "work", "first",
    "well", "way", "even", "new", "want", "because", "any", "these", "give",
    "day", "most", "us", "please", "help", "order", "refund", "delivery",
    "shipping", "package", "service", "account", "is", "are", "was", "am",
}


def _is_english(text: str) -> bool:
    """Heuristic check for English text."""
    if _NON_EN_CHARS.search(text):
        return False
    words = re.findall(r"[a-zA-Záéíóúñüßàâçèêëîïôûù]+", text.lower())
    if not words:
        return True
    non_en_count = sum(1 for w in words if w in _NON_EN_WORDS)
    en_count = sum(1 for w in words if w in _EN_COMMON)
    if non_en_count > en_count:
        return False
    if non_en_count >= 2 and non_en_count * 2 >= en_count:
        return False
    return True


def sample_fresh_threads(
    threads_jsonl: pathlib.Path | str,
    n: int = 320,
    seed: int = 20261028,
    exclude_hashes: Optional[Set[str]] = None,
    dataset_version: str = "routing_v1.0.0",
) -> List[RoutingCase]:
    """Sample fresh first-turn tweets from raw threads.jsonl.

    - Excludes content hashes in exclude_hashes (e.g. Laya train/val + test, or existing pool).
    - Runs PII scrubber (scrub_text) on text.
    - Preserves ~8% non-English tweets (scenario 'ood', language 'other').
    - English tweets get scenario 'normal', language 'en'.
    - Stratified by length bucket.
    - Placeholder expected labels are provisional rule-derived ('other', 'escalate', 'human-standard').
    - Cases tagged with notes='fresh_unlabeled'.
    - Returns deterministically sorted list of RoutingCase.
    """
    if n <= 0:
        return []

    path = pathlib.Path(threads_jsonl)
    if not path.exists():
        raise FileNotFoundError(f"threads.jsonl not found at: {path}")

    exclude = set(exclude_hashes or ())
    seen_hashes: Set[str] = set()

    candidates_en: Dict[str, List[Tuple[Any, str, str, str]]] = collections.defaultdict(list)
    candidates_non_en: Dict[str, List[Tuple[Any, str, str, str]]] = collections.defaultdict(list)

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            turns = [t for t in data.get("turns", []) if t.get("role") == "customer"]
            if not turns:
                continue
            raw_text = turns[0].get("text", "")
            if not raw_text.strip():
                continue
            scrubbed = scrub_text(raw_text)
            if not scrubbed.strip():
                continue

            c_hash = compute_content_hash(subject=None, description=scrubbed)
            raw_hash = compute_content_hash(subject=None, description=raw_text)
            if c_hash in exclude or raw_hash in exclude or c_hash in seen_hashes:
                continue
            seen_hashes.add(c_hash)

            bucket = _compute_length_bucket(scrubbed)
            tid = data.get("thread_id")
            entry = (tid, scrubbed, c_hash, bucket)
            if _is_english(scrubbed):
                candidates_en[bucket].append(entry)
            else:
                candidates_non_en[bucket].append(entry)

    rng = random.Random(seed)

    all_non_en: List[Tuple[Any, str, str, str]] = []
    for b_list in candidates_non_en.values():
        all_non_en.extend(b_list)
    all_non_en.sort(key=lambda x: x[2])  # deterministic ordering by hash

    target_non_en = min(round(n * 0.08), len(all_non_en))
    sampled_non_en = rng.sample(all_non_en, target_non_en) if target_non_en > 0 else []

    target_en = n - len(sampled_non_en)
    sampled_en: List[Tuple[Any, str, str, str]] = []

    en_buckets = sorted(candidates_en.keys())
    total_en_candidates = sum(len(candidates_en[b]) for b in en_buckets)

    if total_en_candidates > 0 and target_en > 0:
        remaining_en = min(target_en, total_en_candidates)
        for idx, b in enumerate(en_buckets):
            b_candidates = list(candidates_en[b])
            b_candidates.sort(key=lambda x: x[2])
            if idx == len(en_buckets) - 1:
                quota = remaining_en
            else:
                quota = round(remaining_en * (len(b_candidates) / total_en_candidates))
                quota = min(quota, remaining_en)
            take = min(quota, len(b_candidates))
            if take > 0:
                sampled_b = rng.sample(b_candidates, take)
                sampled_en.extend(sampled_b)
                remaining_en -= len(sampled_b)
                total_en_candidates -= len(b_candidates)

    cases: List[RoutingCase] = []

    # English cases
    for tid, desc, chash, bucket in sampled_en:
        t_id = make_ticket_id("kaggle_twcs", chash)
        exp = Expected(
            intent="other",
            acceptable_intents=("other",),
            escalate=True,
            decision="escalate",
            queue="human-standard",
            sentiment="neutral",
            urgency=1,
        )
        style = Style(register="casual", length_bucket=bucket)
        case = RoutingCase(
            ticket_id=t_id,
            content_hash=chash,
            dataset_version=dataset_version,
            split="pool",
            created_at="2026-10-28T00:00:00Z",
            subject=None,
            description=desc,
            channel="twitter",
            language="en",
            pii_scrubbed=True,
            expected=exp,
            difficulty="medium",
            scenarios=("normal",),
            style=style,
            source="real",
            source_dataset="kaggle_twcs",
            source_ref=f"twcs_thread_{tid}",
            ground_truth_source="rule_derived",
            label_status="provisional",
            verified_fields=(),
            annotations=(),
            adjudication=None,
            group_id=f"grp-{t_id}",
            parent_ticket_id=None,
            perturbation=None,
            failure_ref=None,
            notes="fresh_unlabeled",
        )
        cases.append(case)

    # Non-English cases
    for tid, desc, chash, bucket in sampled_non_en:
        t_id = make_ticket_id("kaggle_twcs", chash)
        exp = Expected(
            intent="other",
            acceptable_intents=("other",),
            escalate=True,
            decision="escalate",
            queue="human-standard",
            sentiment="neutral",
            urgency=1,
        )
        style = Style(register="casual", length_bucket=bucket)
        case = RoutingCase(
            ticket_id=t_id,
            content_hash=chash,
            dataset_version=dataset_version,
            split="pool",
            created_at="2026-10-28T00:00:00Z",
            subject=None,
            description=desc,
            channel="twitter",
            language="other",
            pii_scrubbed=True,
            expected=exp,
            difficulty="medium",
            scenarios=("ood",),
            style=style,
            source="real",
            source_dataset="kaggle_twcs",
            source_ref=f"twcs_thread_{tid}",
            ground_truth_source="rule_derived",
            label_status="provisional",
            verified_fields=(),
            annotations=(),
            adjudication=None,
            group_id=f"grp-{t_id}",
            parent_ticket_id=None,
            perturbation=None,
            failure_ref=None,
            notes="fresh_unlabeled",
        )
        cases.append(case)

    cases.sort(key=lambda c: c.ticket_id)
    return cases

