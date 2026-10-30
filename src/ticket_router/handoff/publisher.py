"""Pub/Sub REST API and fake publishers for HumanHandoff workflow (REQ-AGENT-030).

# REQ-AGENT-030: Shared HumanHandoff Workflow
# REQ-AUTH-006: mTLS-Aware Google API Transports
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from ticket_router.common.google_http import create_google_session, to_mtls_url

logger = logging.getLogger(__name__)

DEFAULT_HUMAN_QUEUE_TOPIC = "human-queue"


@runtime_checkable
class PublisherProtocol(Protocol):
    """Protocol for Pub/Sub publishing."""

    def publish(
        self,
        topic: str,
        data: bytes,
        attributes: Optional[Dict[str, str]] = None,
        ordering_key: Optional[str] = None,
    ) -> str:
        """Publish a message to topic and return message_id."""
        ...


class FakePublisher:
    """Hermetic in-memory publisher tracking published messages for testing (REQ-AGENT-030)."""

    def __init__(self, fail_times: int = 0, always_fail: bool = False) -> None:
        self.published_messages: list[Dict[str, Any]] = []
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.call_count = 0

    def publish(
        self,
        topic: str,
        data: bytes,
        attributes: Optional[Dict[str, str]] = None,
        ordering_key: Optional[str] = None,
    ) -> str:
        self.call_count += 1
        if self.always_fail or self.call_count <= self.fail_times:
            raise RuntimeError("Simulated publish failure")
        msg_id = f"fake-msg-{len(self.published_messages) + 1}"
        self.published_messages.append({
            "topic": topic,
            "data": data,
            "attributes": attributes or {},
            "ordering_key": ordering_key,
            "message_id": msg_id,
        })
        return msg_id


class PubSubRestPublisher:
    """Pub/Sub REST API publisher supporting mTLS agent identity rewriting (REQ-AUTH-006, REQ-AGENT-030)."""

    def __init__(
        self,
        project: Optional[str] = None,
        topic: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.project = (
            project
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("PROJECT_ID", "")
        )
        self.topic = topic or os.environ.get("HUMAN_QUEUE_TOPIC", DEFAULT_HUMAN_QUEUE_TOPIC)
        if session is not None:
            self.session = session
            self.is_mtls = bool(getattr(session, "is_mtls", False))
        else:
            self.session, self.is_mtls = create_google_session()

    def publish(
        self,
        topic: Optional[str] = None,
        data: bytes = b"",
        attributes: Optional[Dict[str, str]] = None,
        ordering_key: Optional[str] = None,
        timeout: float = 1.5,
    ) -> str:
        target_topic = topic or self.topic
        if not self.project:
            raise ValueError("project must be specified or set via GOOGLE_CLOUD_PROJECT")
        raw_url = f"https://pubsub.googleapis.com/v1/projects/{self.project}/topics/{target_topic}:publish"
        url = to_mtls_url(raw_url, self.is_mtls)

        b64_data = base64.b64encode(data).decode("ascii")
        msg_obj: Dict[str, Any] = {"data": b64_data}
        if attributes:
            msg_obj["attributes"] = {str(k): str(v) for k, v in attributes.items()}
        if ordering_key:
            msg_obj["orderingKey"] = ordering_key

        body = {"messages": [msg_obj]}
        resp = self.session.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        resp_data = resp.json()
        message_ids = resp_data.get("messageIds", [])
        return str(message_ids[0]) if message_ids else ""
