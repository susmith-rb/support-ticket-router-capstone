"""PII tokenization and reversible typed token mapping.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

WITHHELD_VALUE = "<withheld>"
WITHHELD_INFO_TYPES = frozenset({"CREDIT_CARD_NUMBER"})

NEUTRAL_PHRASES: Dict[str, str] = {
    "EMAIL_ADDRESS": "your email address",
    "PHONE_NUMBER": "your phone number",
    "PERSON_NAME": "your name",
    "CREDIT_CARD_NUMBER": "your card",
    "STREET_ADDRESS": "your address",
    "US_SOCIAL_SECURITY_NUMBER": "your social security number",
    "IBAN_CODE": "your account number",
    "ORDER_ID": "your order number",
}

TOKEN_PATTERN = re.compile(r"\[([A-Z_]+)(?:_\d+)?\]")


def tokenize_pii(
    text: str,
    findings: Sequence[Any],
) -> Tuple[str, Dict[str, str]]:
    """Replace SDP finding spans with reversible typed tokens [<INFO_TYPE>_<n>].

    # REQ-STORE-009:
    # 1. Number per info_type in order of appearance in text.
    # 2. Reuse the same token for repeated identical values of the same info_type.
    # 3. Return (tokenized_text, mapping) where mapping is {token: original_value}.
    # 4. For CREDIT_CARD_NUMBER, the token is assigned normally ([CREDIT_CARD_NUMBER_n]),
    #    but the mapping stores '<withheld>' instead of the real card digits to keep
    #    the system strictly out of PCI scope (TDD §4.2 C4).
    # 5. Non-card tokens are completely reversible: replacing token -> value restores
    #    the original text.
    """
    if not text or not findings:
        return text, {}

    # Extract valid spans: (start, end, info_type)
    extracted_spans: List[Tuple[int, int, str]] = []
    for f in findings:
        # Support dict or dataclass/object (e.g. SdpFinding)
        if isinstance(f, dict):
            info_type_raw = f.get("infoType") or f.get("info_type") or "PII"
            loc = f.get("location") or {}
            if isinstance(loc, dict):
                rng = loc.get("codepointRange") or loc.get("codepoint_range") or {}
                if isinstance(rng, dict):
                    start = rng.get("start", 0)
                    end = rng.get("end")
                else:
                    continue
            else:
                continue
        else:
            info_type_raw = getattr(f, "info_type", None) or getattr(f, "infoType", "PII")
            loc = getattr(f, "location", None)
            if isinstance(loc, dict):
                rng = loc.get("codepointRange") or loc.get("codepoint_range") or {}
                start = rng.get("start", 0)
                end = rng.get("end")
            else:
                continue

        try:
            start_int = int(start if start is not None else 0)
            end_int = int(end) if end is not None else -1
        except (ValueError, TypeError):
            continue

        if 0 <= start_int < end_int <= len(text):
            clean_info = str(info_type_raw).strip().upper()
            extracted_spans.append((start_int, end_int, clean_info))

    if not extracted_spans:
        return text, {}

    # Sort spans by start asc, end desc
    extracted_spans.sort(key=lambda s: (s[0], -s[1]))

    # Filter overlapping spans (keep the earlier / longer span)
    non_overlapping: List[Tuple[int, int, str, str]] = []
    last_end = 0
    for start, end, info_type in extracted_spans:
        if start < last_end:
            # Overlapping span; ignore to prevent text corruption
            continue
        val = text[start:end]
        non_overlapping.append((start, end, info_type, val))
        last_end = end

    # Assign tokens per info_type in order of appearance, reusing for identical values
    type_counts: Dict[str, int] = {}
    seen_tokens: Dict[Tuple[str, str], str] = {}
    mapping: Dict[str, str] = {}

    resolved_spans: List[Tuple[int, int, str]] = []
    for start, end, info_type, val in non_overlapping:
        key = (info_type, val)
        if key in seen_tokens:
            token = seen_tokens[key]
        else:
            current_count = type_counts.get(info_type, 0) + 1
            type_counts[info_type] = current_count
            token = f"[{info_type}_{current_count}]"
            seen_tokens[key] = token

            if info_type in WITHHELD_INFO_TYPES:
                mapping[token] = WITHHELD_VALUE
            else:
                mapping[token] = val

        resolved_spans.append((start, end, token))

    # Reconstruct tokenized text
    chunks: List[str] = []
    curr = 0
    for start, end, token in resolved_spans:
        chunks.append(text[curr:start])
        chunks.append(token)
        curr = end
    chunks.append(text[curr:])
    tokenized_text = "".join(chunks)

    return tokenized_text, mapping


def restore_pii_tokens(
    text: Optional[str],
    pii_mapping: Optional[Dict[str, str]] = None,
) -> str:
    """Restore in-memory PII tokens for customer-facing presentation in the same turn.

    # REQ-STORE-009:
    # 1. Tokens matching in-memory pii_mapping are replaced with original values.
    # 2. Exception: Mapping values '<withheld>' must NOT be inserted; drop/neutralize instead
    #    (e.g. replace [CREDIT_CARD_NUMBER_1] with 'your card').
    # 3. Any token without a mapping is replaced with a neutral phrase
    #    (e.g. '[EMAIL_ADDRESS_1]' -> 'your email address').
    # 4. Clean duplicate 'your your' phrasing if a token was preceded by 'your'.
    """
    if not text:
        return "" if text is None else text

    mapping = pii_mapping or {}

    def _replace_token(match: re.Match[str]) -> str:
        token = match.group(0)
        info_type = match.group(1).upper()
        neutral = NEUTRAL_PHRASES.get(info_type, f"your {info_type.lower().replace('_', ' ')}")

        if token in mapping:
            val = mapping[token]
            if val == WITHHELD_VALUE or val == "<withheld>":
                return neutral
            return str(val)

        # Unmapped token -> neutral phrase
        return neutral

    restored = TOKEN_PATTERN.sub(_replace_token, text)
    # Deduplicate any 'your your' / 'Your your' resulting from replacement
    restored = re.sub(r"\b([Yy]our)\s+your\b", r"\1", restored)
    return restored

