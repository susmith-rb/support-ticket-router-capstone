# services/Dockerfile — Unified container image for Cloud Run agent and MCP services.
# Implements REQ-AGENT-001, REQ-AGENT-009, REQ-AGENT-010.
#
# Services selected by runtime environment variable SERVICE:
#   - SERVICE=ticket_db     (ticket-db-mcp server)
#   - SERVICE=order_lookup  (order-lookup-mcp server)
#   - SERVICE=human_queue   (human-queue agent server)

FROM python:3.12-slim

# System configuration and security hardening
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    SERVICE=ticket_db

WORKDIR /app

# Install minimal OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

# Copy packaging configuration and core sources
COPY pyproject.toml README.md /app/
COPY src/ /app/src/

# Install package with runtime, db, and agents extras
RUN pip install --no-cache-dir .[runtime,db,agents]

# Build-time import check: fail image build immediately if any service import is broken
RUN python -c "import ticket_router.serve, ticket_router.mcp.ticket_db, ticket_router.mcp.order_lookup, ticket_router.agents.human_queue, ticket_router.agents.faq_resolver.app; print('All agent & MCP services import cleanly')"

# Switch to non-root user
USER appuser

EXPOSE 8080

# Execute entrypoint module routing to target service via SERVICE environment variable
CMD ["python", "-m", "ticket_router.serve"]
