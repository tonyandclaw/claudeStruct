"""Plain dataclasses that mirror the server's response schemas.

Kept in lock-step with `src/claudestruct/server/schema.py`. The
SDK doesn't import Pydantic; if your application wants validation
beyond "did the JSON parse", layer it on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class KeyMetadata:
    key_id: str
    name: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


@dataclass
class RunRow:
    run_id: str
    started_at: str | None
    ended_at: str | None
    task: str
    model: str
    effort: str | None
    reason: str
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    cache_warnings: list[str]


@dataclass
class DashboardResponse:
    runs: list[RunRow]


@dataclass
class AuthorRollup:
    user_id: int
    email: str
    runs: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


@dataclass
class TaskRollup:
    task: str
    runs: int
    cost_usd: float


@dataclass
class TeamDashboardResponse:
    org_id: int
    org_slug: str
    total_runs: int
    total_cost_usd: float
    by_author: list[AuthorRollup]
    by_task: list[TaskRollup]
    recent: list[RunRow]


@dataclass
class CreateRunResponse:
    run_id: str
    status: str
    note: str | None = None


def _author_from_dict(d: dict[str, Any]) -> AuthorRollup:
    return AuthorRollup(**d)


def _task_from_dict(d: dict[str, Any]) -> TaskRollup:
    return TaskRollup(**d)


def _run_row_from_dict(d: dict[str, Any]) -> RunRow:
    return RunRow(**d)


def team_dashboard_from_dict(d: dict[str, Any]) -> TeamDashboardResponse:
    """Build a TeamDashboardResponse from the JSON the server returns."""
    return TeamDashboardResponse(
        org_id=d["org_id"],
        org_slug=d["org_slug"],
        total_runs=d["total_runs"],
        total_cost_usd=d["total_cost_usd"],
        by_author=[_author_from_dict(a) for a in d["by_author"]],
        by_task=[_task_from_dict(t) for t in d["by_task"]],
        recent=[_run_row_from_dict(r) for r in d["recent"]],
    )


def dashboard_from_dict(d: dict[str, Any]) -> DashboardResponse:
    return DashboardResponse(
        runs=[_run_row_from_dict(r) for r in d.get("runs", [])],
    )


def key_metadata_from_dict(d: dict[str, Any]) -> KeyMetadata:
    return KeyMetadata(**d)
