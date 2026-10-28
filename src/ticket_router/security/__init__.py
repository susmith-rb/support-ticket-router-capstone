"""Security and sanitization package for Support Ticket Router.

# REQ-MA-002: Regional Keep-Alive REST Client, Typed Verdicts & Measured Timeout
"""

from ticket_router.security.model_armor import (
    FakeModelArmorClient,
    FilterVerdict,
    ModelArmorClient,
    ModelArmorClientProtocol,
    ModelArmorVerdict,
    SdpFinding,
    create_model_armor_client,
)

__all__ = [
    "FakeModelArmorClient",
    "FilterVerdict",
    "ModelArmorClient",
    "ModelArmorClientProtocol",
    "ModelArmorVerdict",
    "SdpFinding",
    "create_model_armor_client",
]
