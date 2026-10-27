"""Ticket DB MCP server package.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

from ticket_router.mcp.ticket_db.schemas import get_tool_schemas
from ticket_router.mcp.ticket_db.server import app, create_app

__all__ = ["app", "create_app", "get_tool_schemas"]
