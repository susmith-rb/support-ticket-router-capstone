"""Cloud SQL Python Connector integration for PostgreSQL.

Implements REQ-STORE-002: Secure IAM-authenticated TLS connection over public IP
without requiring authorized networks, compliant with org policy.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ticket_router.common.deploy_defaults import get_deploy_default

logger = logging.getLogger(__name__)

try:
    from google.cloud.sql.connector import Connector, IPTypes

    _HAS_CONNECTOR = True
except ImportError:
    Connector = None
    IPTypes = None
    _HAS_CONNECTOR = False


class CloudSqlConnectionManager:
    """Manages Cloud SQL connection pool using cloud-sql-python-connector and pg8000."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        instance_name: str = "ticket-store",
        database: str = "support",
        user: Optional[str] = None,
        password: Optional[str] = None,
        enable_iam_auth: bool = True,
        ip_type: Optional[Any] = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        if not _HAS_CONNECTOR:
            raise ImportError(
                "Cloud SQL connector dependencies are not installed. "
                "Install with: pip install '.[db]'"
            )

        resolved_project = (
            project_id
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or get_deploy_default("project")
        )
        resolved_region = (
            region
            or os.environ.get("GOOGLE_CLOUD_REGION")
            or get_deploy_default("region")
        )
        self.instance_connection_name = f"{resolved_project}:{resolved_region}:{instance_name}"
        self.database = database
        self.user = user
        self.password = password
        self.enable_iam_auth = enable_iam_auth
        if ip_type is not None:
            self.ip_type = ip_type
        elif IPTypes is not None:
            self.ip_type = IPTypes.PUBLIC
        else:
            self.ip_type = "PUBLIC"
        self.connect_timeout_s = connect_timeout_s
        self._connector: Optional[Connector] = None

    def _get_connector(self) -> Connector:
        if self._connector is None:
            self._connector = Connector(timeout=int(self.connect_timeout_s))
        return self._connector

    def get_connection(self):
        """Create and return a raw database connection via Cloud SQL Connector."""
        connector = self._get_connector()
        connect_args = {
            "instance_connection_string": self.instance_connection_name,
            "driver": "pg8000",
            "db": self.database,
            "ip_type": self.ip_type,
            "enable_iam_auth": self.enable_iam_auth,
            "timeout": int(self.connect_timeout_s),
        }
        if self.user:
            connect_args["user"] = self.user
        if self.password:
            connect_args["password"] = self.password

        return connector.connect(**connect_args)

    def close(self) -> None:
        """Close the underlying connector and cleanup resources."""
        if self._connector is not None:
            self._connector.close()
            self._connector = None
