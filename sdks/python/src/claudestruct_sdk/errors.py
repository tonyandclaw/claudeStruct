"""Exception hierarchy for the SDK.

Every status code ≥ 400 from the server lands as an `ApiError` or
a more specific subclass. The SDK does not retry — that's the
caller's call — but it does translate HTTP status into a typed
exception so application code can `except AuthError:` cleanly.
"""
from __future__ import annotations


class ApiError(Exception):
    """Base class for any non-2xx response."""

    def __init__(self, status_code: int, detail: str | None = None) -> None:
        super().__init__(f"{status_code}: {detail or '<no detail>'}")
        self.status_code = status_code
        self.detail = detail


class AuthError(ApiError):
    """401 — bearer token invalid / revoked / expired."""


class ForbiddenError(ApiError):
    """403 — role lacks the required level for this endpoint."""


class NotFoundError(ApiError):
    """404 — resource doesn't exist OR exists in another tenant
    (the server returns 404 for both — see the threat-model doc
    for the rationale)."""


class BudgetExceededError(ApiError):
    """402 — the org has hit its monthly token / cost cap. Inspect
    `response.body` for `used_tokens`, `cap_tokens`, `period_end`,
    `tier`."""


class ServerError(ApiError):
    """5xx — the daemon is unhappy. Try again or page on-call."""
