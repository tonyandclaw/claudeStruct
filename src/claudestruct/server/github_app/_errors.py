"""Errors raised by the GitHub App integration.

Lives in its own module so other submodules of `github_app/` can
import it without creating a circular dependency back through
``__init__.py``.
"""
from __future__ import annotations


class GitHubAppError(RuntimeError):
    """Raised when the App ↔ GitHub conversation fails. Caught by
    callers that want to log + skip the outbound action without
    crashing the request that triggered it."""
