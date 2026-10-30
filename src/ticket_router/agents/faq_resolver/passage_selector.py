"""passage_selector.py — Deterministic passage selection for static grounded fallback.

# REQ-AGENT-028: Grounded Static-Passage Fallback When Answer Generation Stalls.
Selects 1-3 consecutive sentences (<= 600 chars) from the top-ranked retrieved
FAQ article with the highest lexical overlap with the query. Pure functions only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Lightweight stopword list for support queries (no heavy NLP / LLM dependencies)
_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "can't", "cannot", "could",
    "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down",
    "during", "each", "few", "for", "from", "further", "had", "hadn't", "has",
    "hasn't", "have", "haven't", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "isn't", "it",
    "its", "itself", "me", "more", "most", "my", "myself", "no", "nor", "not", "of",
    "off", "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "she", "should", "shouldn't", "so", "some", "such",
    "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "wasn't", "we", "were", "weren't", "what", "when", "where", "which",
    "while", "who", "whom", "why", "with", "won't", "would", "wouldn't", "you",
    "your", "yours", "yourself", "yourselves",
}


def _tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase alphanumeric words."""
    if not text:
        return []
    return re.findall(r"\b[a-z0-9]+(?:'[a-z0-9]+)?\b", text.lower())


def _stem(token: str) -> str:
    """Light suffix stripping for plural and tense normalization."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            if suffix == "ies":
                return token[:-3] + "y"
            return token[:-len(suffix)]
    return token


def _extract_sentence_spans(text: str) -> List[Tuple[str, int, int]]:
    """Extract list of (sentence_text, start_offset, end_offset) spans from text.

    Splits on sentence-ending punctuation (.!?) followed by whitespace, or newlines,
    preserving exact offsets into the original text for verbatim extraction.
    """
    if not text:
        return []

    spans: List[Tuple[str, int, int]] = []
    # Match sentences ending with [.!?] or newline or end-of-string
    for match in re.finditer(r"[^\s.!?\n][^.!?\n]*?(?:[.!?](?=\s|\n|$)|(?=\n|$))", text):
        s = match.group(0).strip()
        if s:
            spans.append((s, match.start(), match.end()))
    return spans


def select_static_passage(
    query: str,
    article_content: str,
    max_sentences: int = 3,
    max_chars: int = 600,
) -> str:
    """Select 1-3 consecutive sentences (<= max_chars) with highest lexical overlap.

    Args:
        query: Customer support question or inquiry.
        article_content: Full text of retrieved article.
        max_sentences: Maximum consecutive sentences to combine (default 3).
        max_chars: Maximum character length of returned passage (default 600).

    Returns:
        Verbatim passage string extracted directly from article_content.
    """
    if not article_content or not article_content.strip():
        return ""

    spans = _extract_sentence_spans(article_content)
    if not spans:
        return article_content[:max_chars].strip()

    # Tokenize query and remove stopwords
    q_tokens = _tokenize(query)
    q_stems = {_stem(t) for t in q_tokens if t not in _STOPWORDS}
    if not q_stems:
        # Fall back to all tokens if query was entirely stopwords
        q_stems = {_stem(t) for t in q_tokens}

    candidates: List[Tuple[int, float, int, int, int, str]] = []

    # Evaluate all candidate windows of length 1 to max_sentences
    for k in range(1, min(max_sentences, len(spans)) + 1):
        for i in range(len(spans) - k + 1):
            start = spans[i][1]
            end = spans[i + k - 1][2]
            passage = article_content[start:end].strip()

            if len(passage) > max_chars:
                if k == 1:
                    passage = passage[:max_chars].strip()
                else:
                    continue

            p_tokens = _tokenize(passage)
            p_stems = {_stem(t) for t in p_tokens}
            matched = q_stems.intersection(p_stems)
            overlap_count = len(matched)
            density = overlap_count / max(len(p_tokens), 1)

            # Sort tuple: (-overlap_count, -density, length, start_idx, sentence_count, passage)
            candidates.append((overlap_count, density, len(passage), i, k, passage))

    if not candidates:
        return article_content[:max_chars].strip()

    # Rank: highest overlap first, then highest density, then shortest length, then earliest position
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3], c[4]))
    best = candidates[0]

    # If no window had any query overlap (overlap_count == 0), take opening 1-3 sentences
    if best[0] == 0:
        for k in range(min(max_sentences, len(spans)), 0, -1):
            start = spans[0][1]
            end = spans[k - 1][2]
            passage = article_content[start:end].strip()
            if len(passage) <= max_chars:
                return passage
        return spans[0][0][:max_chars].strip()

    return best[5]


def build_static_fallback_payload(
    query: str,
    top_doc: Dict[str, Any],
    max_sentences: int = 3,
    max_chars: int = 600,
) -> Dict[str, Any]:
    """Construct static grounded answer payload matching CitationValidator contract.

    Args:
        query: Customer inquiry.
        top_doc: Retrieved top-ranked document dict with 'id', 'title', 'content'.
        max_sentences: Maximum sentences for passage (default 3).
        max_chars: Maximum character limit for passage (default 600).

    Returns:
        Dict with 'answer', 'citations', and 'passage'.
    """
    doc_id = str(top_doc.get("id") or top_doc.get("doc_id") or "").strip()
    title = str(top_doc.get("title") or "").strip()
    content = str(top_doc.get("content") or top_doc.get("body") or top_doc.get("text") or "").strip()

    passage = select_static_passage(
        query=query,
        article_content=content,
        max_sentences=max_sentences,
        max_chars=max_chars,
    )
    if not passage:
        passage = content[:max_chars].strip() if content else title

    if title and title.lower() != passage.lower():
        answer = f"{title}\n\n{passage}"
    else:
        answer = passage

    quote = passage

    return {
        "answer": answer,
        "citations": [{"doc_id": doc_id, "quote": quote}],
        "passage": passage,
    }
