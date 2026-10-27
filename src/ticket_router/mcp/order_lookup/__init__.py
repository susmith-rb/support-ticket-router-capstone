"""Order Lookup MCP server package.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

from ticket_router.mcp.order_lookup.schemas import get_tool_schemas
from ticket_router.mcp.order_lookup.server import app, create_app

__all__ = ["app", "create_app", "get_tool_schemas"]
