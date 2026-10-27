"""Schemas and contracts for order-lookup-mcp tool endpoints.

Implements REQ-AGENT-010: Typed JSON contracts, schema validation, and BOLA/IDOR protection.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class GetOrderToolRequest(BaseModel):
    """MCP tool input payload for get_order.

    In accordance with REQ-AGENT-010, AGENTS.md rule 7, and TDD §8.4, customer_id
    is NEVER accepted as an untrusted tool parameter. It is strictly derived
    from verified session claims / caller headers.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., description="Target order identifier (e.g. ORD-1001)")


class GetOrderHttpRequest(BaseModel):
    """HTTP client payload for /get_order supporting HttpOrderLookupClient wire format."""

    order_id: str = Field(..., description="Target order identifier")
    customer_id: Optional[str] = Field(default=None, description="Customer ID passed from verified agent context")


class GetOrderResponse(BaseModel):
    """Output payload for get_order tool."""

    order_id: str = Field(..., description="Order identifier")
    status: str = Field(..., description="Order lifecycle status (e.g. DELIVERED, SHIPPED, PROCESSING)")
    items: List[Any] = Field(default_factory=list, description="List of items in the order")
    tracking_number: str = Field(default="", description="Carrier shipment tracking number")
    promised_delivery: str = Field(default="", description="Promised or estimated delivery timestamp")
    delivery_address_redacted: str = Field(default="[ADDRESS_REDACTED]", description="Redacted delivery address")


def get_tool_schemas() -> Dict[str, Any]:
    """Return JSON schema for order-lookup-mcp get_order tool per REQ-AGENT-010."""
    return {
        "get_order": {
            "name": "get_order",
            "description": "Fetch order details for the authenticated customer. Customer ID is derived strictly from verified session claims.",
            "parameters": GetOrderToolRequest.model_json_schema(),
            "returns": GetOrderResponse.model_json_schema(),
        }
    }
