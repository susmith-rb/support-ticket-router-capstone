"""Multi-intent question deconstruction and sub-question definitions.

# REQ-SAFE-005: Multi-Intent Sentence Deconstruction and Dual-Pipeline Grounded Resolution
# REQ-TURN-001: Natural Language Interpretation via Gemini Flash Structured Output
# REQ-TURN-004: Deterministic Enforcement of Turn Acts and Security Refusals
"""

from __future__ import annotations

from typing import Dict, List, Optional


def classify_sub_intent(text: str) -> Optional[str]:
    """Retired in favor of Gemini Flash Turn Interpreter (REQ-TURN-001)."""
    return None


def detect_sub_questions(text: str) -> List[Dict[str, str]]:
    """Retired in favor of Gemini Flash Turn Interpreter (REQ-TURN-001)."""
    return []
