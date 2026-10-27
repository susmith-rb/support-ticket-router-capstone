"""Cloud SQL Python Connector integration for PostgreSQL.

Implements REQ-STORE-002: Secure IAM-authenticated TLS connection over public IP
without requiring authorized networks, compliant with org policy.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from google.cloud.sql.connector import Connector, IPTypes
    import pg8000.dbapi

    _HAS_CONNECTOR = True
except ImportError:
    Connector = None
    IPTypes = None
    pg8000 = None
    _HAS_CONNECTOR = False


class CloudSqlConnectionManager:
    """Manages Cloud SQL connection pool using cloud-sql-python-connector and pg8000."""

    def __init__(
        self,
        project_id: str = "susmith-fde-ticket-router",
        region: str = "us-central1",
        instance_name: str = "ticket-store",
        database: str = "support",
        user: Optional[str] = None,
        password: Optional[str] = None,
        enable_iam_auth: bool = True,
        ip_type: Optional[Any] = None,
    ) -> None:
        if not _HAS_CONNECTOR:
            raise ImportError(
                "Cloud SQL connector dependencies are not installed. "
                "Install with: pip install '.[db]'"
            )

        self.instance_connection_name = f"{project_id}:{region}:{instance_name}"
        self.database = database
        self.user = user
        self.password = password
        self.enable_iam_auth = enable_iam_auth
        self.ip_type = ip_type or IPTypes.PUBLIC
        self._connector: Optional[Connector] = None

    def _get_connector(self) -> Connector:
        if self._connector is None:
            self._connector = Connector()
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
