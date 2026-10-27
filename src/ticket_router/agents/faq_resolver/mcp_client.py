"""mcp_client.py — Order Lookup MCP client with strict BOLA/IDOR protection.

REQ-AGENT-010, REQ-AGENT-012, AGENTS.md rule 7, TDD §8.4.
Enforces BOLA/IDOR protection: customer_id is strictly derived from verified session
claims and NEVER accepted from untrusted model/tool arguments.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

from ticket_router.agents.faq_resolver.protocols import OrderLookupProtocol
from ticket_router.common.auth import get_access_token
from ticket_router.common.resilience import (
    CircuitBreaker,
    ORDER_LOOKUP_MCP_TIMEOUT_S,
)


class BolaViolationError(PermissionError):
    """Raised when an untrusted customer_id argument attempts to override verified session claims."""


class HttpOrderLookupClient(OrderLookupProtocol):
    """HTTP client for order-lookup-mcp Cloud Run service."""

    def __init__(
        self,
        base_url: str = "http://order-lookup-mcp:8080",
        timeout_s: float = ORDER_LOOKUP_MCP_TIMEOUT_S,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.circuit_breaker = circuit_breaker

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

        def _do_request() -> Dict[str, Any]:
            token = get_access_token()
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data

        try:
            if self.circuit_breaker is not None:
                return self.circuit_breaker.call_with_timeout(_do_request, timeout_s=self.timeout_s)
            return _do_request()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(f"Order '{order_id}' not found for customer.") from exc
            if exc.code == 403:
                raise BolaViolationError("Access denied: order does not belong to verified customer.") from exc
            raise


class FakeOrderLookupClient(OrderLookupProtocol):
    """Hermetic in-memory mock client for order lookup."""

    def __init__(self, orders: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        # Maps order_id -> order_dict (which must contain 'customer_id')
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
