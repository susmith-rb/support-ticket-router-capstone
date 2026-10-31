"""Vertex AI Agent Engine Memory Bank integration package.

# REQ-AGENT-036: Vertex AI Agent Engine Memory Bank Integration
"""

from ticket_router.memory.client import (
    DEFAULT_MEMORY_BANK_ENGINE,
    DEFAULT_RETRIEVAL_TIMEOUT_S,
    DEFAULT_TOP_K,
    DEFAULT_TTL_S,
    MAX_MEMORY_SUMMARY_CHARS,
    BaseMemoryBankClient,
    FakeMemoryBankClient,
    VertexMemoryBankClient,
    is_memory_bank_enabled,
    render_memory_summary,
)

__all__ = [
    "DEFAULT_MEMORY_BANK_ENGINE",
    "DEFAULT_RETRIEVAL_TIMEOUT_S",
    "DEFAULT_TOP_K",
    "DEFAULT_TTL_S",
    "MAX_MEMORY_SUMMARY_CHARS",
    "BaseMemoryBankClient",
    "FakeMemoryBankClient",
    "VertexMemoryBankClient",
    "is_memory_bank_enabled",
    "render_memory_summary",
]
