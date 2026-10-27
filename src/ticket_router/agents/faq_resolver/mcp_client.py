"""mcp_client.py — Order Lookup MCP client with strict BOLA/IDOR protection.

# REQ-AGENT-010, REQ-AGENT-012, AGENTS.md rule 7, TDD §8.4.
# Enforces BOLA/IDOR protection: customer_id is strictly derived from verified session
# claims and NEVER accepted from untrusted model/tool arguments.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional
import urllib.error
import urllib.request
import requests
from requests.adapters import HTTPAdapter

from ticket_router.agents.faq_resolver.protocols import OrderLookupProtocol
from ticket_router.common.idtoken import get_id_token
from ticket_router.common.resilience import (
    CircuitBreaker,
    ORDER_LOOKUP_MCP_TIMEOUT_S,
)


def _create_pooled_session() -> requests.Session:
    """Create thread-safe requests session with keep-alive connection pooling."""
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


class BolaViolationError(PermissionError):
    """Raised when an untrusted customer_id argument attempts to override verified session claims."""


class HttpOrderLookupClient(OrderLookupProtocol):
    """HTTP client for order-lookup-mcp Cloud Run service.

    Sends verified customer context header X-Customer-ID and OIDC Bearer token.
    Uses pooled keep-alive HTTP connections.
    """

    def __init__(
        self,
        base_url: str = "http://order-lookup-mcp:8080",
        timeout_s: float = ORDER_LOOKUP_MCP_TIMEOUT_S,
        circuit_breaker: Optional[CircuitBreaker] = None,
        session: Optional[Any] = None,
        token_provider: Optional[Callable[[str], Optional[str]]] = None,
        audience: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience or self.base_url
        self.timeout_s = timeout_s
        self.circuit_breaker = circuit_breaker
        self.session = session if session is not None else _create_pooled_session()
        self.token_provider = token_provider or get_id_token

    def _execute_http(self, url: str, body: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        """Perform HTTP POST request via pooled session or test urllib adapter."""
        if url.startswith("http://testserver"):
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise LookupError("Order not found for customer.") from exc
                if exc.code == 403:
                    raise BolaViolationError("Access denied: order does not belong to verified customer.") from exc
                raise

        resp = self.session.post(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            timeout=self.timeout_s,
        )
        if resp.status_code == 404:
            raise LookupError("Order not found for customer.")
        if resp.status_code == 403:
            raise BolaViolationError("Access denied: order does not belong to verified customer.")
        resp.raise_for_status()
        return resp.json()

    def get_order(
        self,
        order_id: str,
        customer_id: str,
        untrusted_customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch order details bound strictly to the verified customer_id.

        Raises:
            BolaViolationError: If untrusted_customer_id is provided and differs from customer_id.
            LookupError: If order is not found or does not belong to the verified customer.
        """
        # BOLA / IDOR Invariant Guard
        if untrusted_customer_id is not None and untrusted_customer_id != customer_id:
            raise BolaViolationError(
                f"BOLA/IDOR violation detected: untrusted tool argument '{untrusted_customer_id}' "
                f"does not match verified session customer '{customer_id}'."
            )

        url = f"{self.base_url}/get_order"
        body = {
            "order_id": order_id,
            "customer_id": customer_id,  # Passed strictly from verified context
        }
        headers = {
            "Content-Type": "application/json",
            "X-Customer-ID": customer_id,  # Verified customer context header
        }

        token = None
        try:
            token = self.token_provider(self.audience)
        except Exception:
            token = None
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if self.circuit_breaker is not None:
            return self.circuit_breaker.call(self._execute_http, url, body, headers)
        return self._execute_http(url, body, headers)


class FakeOrderLookupClient(OrderLookupProtocol):
    """Hermetic in-memory mock client for order lookup."""

    def __init__(self, orders: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.orders: Dict[str, Dict[str, Any]] = dict(orders or {})
        self.call_history: list[Dict[str, Any]] = []

    def add_order(
        self,
        order_id: str,
        customer_id: str,
        status: str = "DELIVERED",
        items: Optional[list[str]] = None,
        tracking_number: str = "TRK12345678",
        promised_delivery: str = "2026-10-25",
        delivery_address_redacted: str = "[ADDRESS_1]",
    ) -> None:
        self.orders[order_id] = {
            "order_id": order_id,
            "customer_id": customer_id,
            "status": status,
            "items": items or ["Wireless Headphones"],
            "tracking_number": tracking_number,
            "promised_delivery": promised_delivery,
            "delivery_address_redacted": delivery_address_redacted,
        }

    def get_order(
        self,
        order_id: str,
        customer_id: str,
        untrusted_customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.call_history.append({
            "order_id": order_id,
            "customer_id": customer_id,
            "untrusted_customer_id": untrusted_customer_id,
            "timestamp": time.time(),
        })

        # BOLA / IDOR invariant
        if untrusted_customer_id is not None and untrusted_customer_id != customer_id:
            raise BolaViolationError(
                f"BOLA/IDOR attempt: untrusted arg customer_id='{untrusted_customer_id}' "
                f"rejected; verified session customer_id='{customer_id}'."
            )

        order = self.orders.get(order_id)
        if not order:
            raise LookupError(f"Order '{order_id}' not found.")

        # Assert customer boundary
        if order.get("customer_id") != customer_id:
            # Uniform not-found / BOLA prevention
            raise LookupError(f"Order '{order_id}' not found for customer.")

        # Return order fields without exposing internal customer_id
        return {
            "order_id": order["order_id"],
            "status": order["status"],
            "items": order.get("items", []),
            "tracking_number": order.get("tracking_number", ""),
            "promised_delivery": order.get("promised_delivery", ""),
            "delivery_address_redacted": order.get("delivery_address_redacted", ""),
        }
