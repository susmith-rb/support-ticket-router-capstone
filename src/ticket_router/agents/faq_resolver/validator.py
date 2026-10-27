"""validator.py — Grounded citation validation for FaqResolverAgent.

REQ-AGENT-008: Mandatory Grounded Citations with Non-Hallucination Fallback.
Every answer synthesized by FaqResolverAgent MUST include >= 1 citation whose
quote is verbatim present in the retrieved FAQ article text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union


def norm(s: str) -> str:
    """Normalize text for verbatim citation comparison.

    Strips markdown formatting (*, _, `, #, >), collapses consecutive whitespace,
    and converts to lowercase. Preserves exact alphanumeric substring ordering.
    """
    if not s:
        return ""
    # Strip markdown syntax characters and collapse whitespace
    no_md = re.sub(r"[*_`#>]", "", s)
    return re.sub(r"\s+", " ", no_md).strip().lower()


def is_verbatim_citation(quote: str, doc_text: str) -> bool:
    """Check if quote appears verbatim (modulo whitespace/formatting) in doc_text.

    Returns False if quote or doc_text is empty, or if quote is paraphrased.
    """
    if not quote or not doc_text:
        return False
    norm_q = norm(quote)
    if not norm_q:
        return False
    return norm_q in norm(doc_text)


@dataclass
class Citation:
    """A citation mapping to a retrieved knowledge-base document."""

    doc_id: str
    quote: str

    def to_dict(self) -> Dict[str, str]:
        return {"doc_id": self.doc_id, "quote": self.quote}


@dataclass
class ValidationResult:
    """Result of citation validation against retrieved documents."""

    is_valid: bool
    reason: Optional[str] = None  # "empty_citations", "doc_not_retrieved", "paraphrased_quote"
    details: Optional[str] = None
    citations: List[Citation] = field(default_factory=list)


class CitationValidator:
    """Deterministic citation validator enforcing verbatim grounding.

    Validates that:
    1. At least one citation is present in the synthesized answer.
    2. Every cited doc_id exists in the set of retrieved documents.
    3. Every cited quote is verbatim present in the corresponding retrieved document.
    """

    def validate(
        self,
        answer_payload: Union[Dict[str, Any], Sequence[Dict[str, Any]]],
        retrieved_docs: Dict[str, Any],
    ) -> ValidationResult:
        """Validate citations in answer_payload against retrieved_docs.

        Args:
            answer_payload: Dict containing 'citations' list (or raw list of citations).
            retrieved_docs: Dict mapping doc_id -> doc data (dict or text string).

        Returns:
            ValidationResult with pass/fail and specific failure reasons.
        """
        raw_citations: List[Any] = []
        if isinstance(answer_payload, dict):
            raw_citations = answer_payload.get("citations", [])
        elif isinstance(answer_payload, (list, tuple)):
            raw_citations = list(answer_payload)

        if not raw_citations:
            return ValidationResult(
                is_valid=False,
                reason="empty_citations",
                details="Answer contains zero citations; at least one verbatim citation is mandatory.",
            )

        parsed_citations: List[Citation] = []
        for i, c in enumerate(raw_citations):
            if not isinstance(c, dict):
                return ValidationResult(
                    is_valid=False,
                    reason="invalid_citation_format",
                    details=f"Citation at index {i} is not a valid object: {c}",
                )

            doc_id = (c.get("doc_id") or c.get("document_id") or "").strip()
            quote = (c.get("quote") or "").strip()

            if not doc_id or not quote:
                return ValidationResult(
                    is_valid=False,
                    reason="empty_citations",
                    details=f"Citation at index {i} has empty doc_id ('{doc_id}') or empty quote ('{quote}').",
                )

            # Check doc_id exists in retrieved_docs
            if doc_id not in retrieved_docs:
                return ValidationResult(
                    is_valid=False,
                    reason="doc_not_retrieved",
                    details=f"Citation doc_id '{doc_id}' was not in retrieved articles {list(retrieved_docs.keys())}.",
                )

            # Extract document text
            doc_data = retrieved_docs[doc_id]
            doc_text = ""
            if isinstance(doc_data, str):
                doc_text = doc_data
            elif isinstance(doc_data, dict):
                parts = [
                    str(doc_data[k])
                    for k in ("title", "content", "body", "text")
                    if doc_data.get(k)
                ]
                doc_text = " ".join(parts) if parts else str(doc_data)
            else:
                doc_text = str(doc_data)

            # Check verbatim presence
            if not is_verbatim_citation(quote, doc_text):
                return ValidationResult(
                    is_valid=False,
                    reason="paraphrased_quote",
                    details=f"Quote '{quote}' does not appear verbatim in article '{doc_id}'.",
                )

            parsed_citations.append(Citation(doc_id=doc_id, quote=quote))

        return ValidationResult(
            is_valid=True,
            citations=parsed_citations,
        )
