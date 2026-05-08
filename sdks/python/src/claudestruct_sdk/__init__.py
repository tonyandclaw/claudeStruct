"""claudestruct-sdk — pre-stable Python client for the claudeStruct REST API.

See `README.md` for the design notes. The version is pinned at
``0.0.1a0`` to signal "shape will shift before 0.1.0; pin
exact versions" to consumers.
"""
from __future__ import annotations

from claudestruct_sdk.client import Client
from claudestruct_sdk.errors import (
    ApiError,
    AuthError,
    BudgetExceededError,
    NotFoundError,
)
from claudestruct_sdk.models import (
    DashboardResponse,
    KeyMetadata,
    RunRow,
    TeamDashboardResponse,
)

__version__ = "0.0.1a0"

__all__ = [
    "Client",
    "ApiError",
    "AuthError",
    "BudgetExceededError",
    "NotFoundError",
    "DashboardResponse",
    "KeyMetadata",
    "RunRow",
    "TeamDashboardResponse",
    "__version__",
]
