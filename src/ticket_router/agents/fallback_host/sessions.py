"""In-memory session store for Cloud Run fallback agent host.

# REQ-AGENT-035: Cloud Run Warm Fallback Agent Host (triage-fallback)
# REQ-AGENT-017: Agent Runtime & Vertex AI Agent Engine Deployment

Architectural Note on Degraded State:
When running on Cloud Run (triage-fallback) instead of Vertex AI Agent Engine:
1. Sessions are ephemeral and kept in-memory within the active container replica.
   They are Cloud SQL-free and not synchronized across multiple auto-scaled replicas.
2. Memory Bank (long-term memory extraction, persistence, and recall across sessions)
   is unavailable/degraded in this mode.
3. Callers receive explicit degraded_mode annotations in health check and triage replies.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ticket_router.agents.fallback_host.sessions")

# Degradation documentation constants
SESSION_DEGRADATION_REASON = (
    "Sessions are ephemeral and stored in container memory only (Cloud SQL-free). "
    "Multi-replica sticky routing is not guaranteed."
)
MEMORY_BANK_DEGRADATION_REASON = (
    "Vertex AI Agent Engine Memory Bank is unavailable on Cloud Run fallback host; "
    "long-term cross-session user memory is not extracted or recalled."
)


class InMemorySessionStore:
    """Thread-safe, Cloud SQL-free in-memory session store for Cloud Run fallback."""

    def __init__(self, max_sessions: int = 10000) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._max_sessions = max_sessions

    @property
    def degradation_notice(self) -> Dict[str, str]:
        """Return standardized degradation notice for callers."""
        return {
            "sessions": "degraded_in_memory",
            "sessions_detail": SESSION_DEGRADATION_REASON,
            "memory_bank": "unavailable",
            "memory_bank_detail": MEMORY_BANK_DEGRADATION_REASON,
        }

    def create_session(
        self,
        session_id: Optional[str] = None,
        user_id: str = "anon",
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create or initialize a session in container memory."""
        sid = session_id.strip() if session_id and session_id.strip() else f"sess-{uuid.uuid4().hex[:12]}"
        now = time.time()
        session_data: Dict[str, Any] = {
            "id": sid,
            "session_id": sid,
            "user_id": user_id,
            "state": dict(state or {}),
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "degraded": True,
        }
        with self._lock:
            # Evict oldest if capacity exceeded
            if len(self._sessions) >= self._max_sessions and sid not in self._sessions:
                oldest_key = min(self._sessions, key=lambda k: self._sessions[k].get("updated_at", 0))
                self._sessions.pop(oldest_key, None)
            self._sessions[sid] = session_data

        logger.debug("Created in-memory fallback session %s for user %s", sid, user_id)
        return session_data

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve existing session if present."""
        if not session_id:
            return None
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess:
                sess["updated_at"] = time.time()
                return dict(sess)
        return None

    def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        user_id: str = "anon",
    ) -> Dict[str, Any]:
        """Retrieve session or create new one if not found."""
        if session_id:
            existing = self.get_session(session_id)
            if existing is not None:
                return existing
        return self.create_session(session_id=session_id, user_id=user_id)

    def update_session_state(
        self,
        session_id: str,
        state_updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Update session state dictionary."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            sess["state"].update(state_updates)
            sess["updated_at"] = time.time()
            return dict(sess)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session from memory."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List active in-memory sessions."""
        with self._lock:
            return [dict(s) for s in self._sessions.values()]

    def clear(self) -> None:
        """Clear all sessions (useful for test resets)."""
        with self._lock:
            self._sessions.clear()
