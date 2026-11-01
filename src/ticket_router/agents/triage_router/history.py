"""Constants and configuration for conversation history requests.

# REQ-HIST-001: Deterministic Conversation History Access Detection and Turn Ordering
# REQ-HIST-002: Server-Side Authentication & Cross-Customer History Isolation
# REQ-TURN-001: Natural Language Interpretation via Gemini Flash Structured Output
"""

from __future__ import annotations

# Memory Bank read budget when the customer explicitly asks for their history (REQ-HIST-002);
# larger than the 300 ms per-turn budget used for background context.
HISTORY_MEMORY_TIMEOUT_S = 2.5
