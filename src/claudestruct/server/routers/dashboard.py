"""Read-only dashboard endpoints.

`GET /v1/dashboard` reads the legacy JSONL store (single-user, local
to the daemon's host). `GET /v1/dashboard/team` queries the `runs`
table and rolls up by author + task for the caller's org — the W6.5
multi-user view.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from claudestruct import dashboard as dash_mod
from claudestruct.server import auth as auth_mod
from claudestruct.server.models import (
    Org,
    Role,
    Run,
    RunStatus,
    Team,
    TeamMembership,
    User,
)
from claudestruct.server.schema import (
    AuthorRollup,
    DashboardResponse,
    RunRow,
    TaskRollup,
    TeamDashboardResponse,
)

router = APIRouter(prefix="/v1", tags=["dashboard"])


def _resolve_root(request: Request) -> Path:
    """Project root that the daemon reads from. Set on
    ``app.state.run_root`` by the app factory."""
    return Path(request.app.state.run_root)


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
) -> DashboardResponse:
    """Read the legacy JSONL run store.

    Production-readiness: paginated (`?limit=`, `?offset=`) so a
    historic run-log of 10k+ rows doesn't return a 50 MB JSON in
    one response. Defaults to the most-recent 50 runs starting
    at offset 0; max `limit` is 500 — same ceiling as
    `/v1/dashboard/team` so the frontend can use a single
    pagination component for both.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be between 1 and 500",
        )
    if offset < 0:
        raise HTTPException(
            status_code=400, detail="offset must be >= 0",
        )
    summaries = dash_mod.load_summaries(_resolve_root(request))
    # `load_summaries` already returns most-recent-first; slice
    # AFTER ordering rather than re-sorting the whole list per
    # request.
    page = summaries[offset : offset + limit]
    rows = [
        RunRow(
            run_id=s.run_id,
            started_at=s.started_at,
            ended_at=s.ended_at,
            task=s.task,
            model=s.model,
            effort=s.effort,
            reason=s.reason,
            duration_ms=s.duration_ms,
            input_tokens=s.input_tokens,
            output_tokens=s.output_tokens,
            cache_read_tokens=s.cache_read_tokens,
            cache_creation_tokens=s.cache_creation_tokens,
            cost_usd=round(s.cost_usd, 6),
            cache_warnings=s.cache_warnings,
        )
        for s in page
    ]
    return DashboardResponse(runs=rows)


def _run_to_row(r: Run) -> RunRow:
    """Translate a Run row into the dashboard's RunRow shape."""
    return RunRow(
        run_id=r.run_id,
        started_at=r.started_at.isoformat() if r.started_at else None,
        ended_at=r.ended_at.isoformat() if r.ended_at else None,
        task=r.task,
        model=r.model,
        effort=r.effort,
        reason=r.status,
        duration_ms=r.duration_ms,
        input_tokens=r.input_tokens,
        output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens,
        cache_creation_tokens=r.cache_creation_tokens,
        cost_usd=round(r.cost_usd, 6),
        cache_warnings=[r.error] if r.error else [],
    )


@router.get("/dashboard/team", response_model=TeamDashboardResponse)
def get_team_dashboard(
    request: Request,
    limit: int = 50,
    team: str | None = None,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
) -> TeamDashboardResponse:
    """Org-scoped rollup of runs executed via the daemon (W6.5).

    Three views in one response:

      - `total_runs` / `total_cost_usd` — the headline numbers.
      - `by_author` — leaderboard sorted by spend, descending. Helps
        spot the engineer accidentally burning the budget on `cs plan
        --effort max` against the entire monorepo.
      - `by_task` — task-type breakdown for the donut chart.
      - `recent` — the latest `limit` runs (default 50).

    Only `done` and `failed` runs are counted in the rollups; `queued`
    and `running` rows skew the cost numbers and are visible via
    `recent` if the caller wants live status.

    Pass `?team=<slug>` to scope the rollup to a single intra-org
    team — the response then only counts runs whose author belongs
    to that team. A team slug from a different org returns 404 (not
    403) so the response can't be used to enumerate teams across
    tenants.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")

    factory = request.app.state.session_factory
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.id == principal.org_id)
        ).scalar_one_or_none()
        if org is None:
            # Belt-and-braces: auth verified the principal but the org
            # could have been deleted in flight. Treat as 404.
            raise HTTPException(status_code=404, detail="org not found")

        # Optional team scope. Resolved up-front so the rest of the
        # rollup logic stays a single straight-line read.
        team_user_ids: set[int] | None = None
        if team is not None:
            team_row = session.execute(
                select(Team).where(
                    Team.org_id == org.id, Team.slug == team,
                )
            ).scalar_one_or_none()
            if team_row is None:
                # 404 (not 403) — a team that exists in another org
                # must not be distinguishable from a team that doesn't
                # exist at all. Otherwise the endpoint becomes a
                # cross-tenant team-slug enumeration oracle.
                raise HTTPException(status_code=404, detail="team not found")
            team_user_ids = {
                m.user_id for m in session.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team_row.id
                    )
                ).scalars()
            }
            if not team_user_ids:
                # Empty team — short-circuit with an empty payload
                # rather than running a `Run.user_id IN ()` query
                # (some DBs choke on empty IN clauses).
                return TeamDashboardResponse(
                    org_id=org.id,
                    org_slug=org.slug,
                    total_runs=0,
                    total_cost_usd=0.0,
                    by_author=[],
                    by_task=[],
                    recent=[],
                )

        # Materialise once: list[Run] keeps the rollup logic readable
        # and avoids three identical SQL filters. For a single org's
        # daily volume this fits comfortably in memory.
        terminal_states = (RunStatus.done.value, RunStatus.failed.value)
        run_query = select(Run).where(
            Run.org_id == org.id, Run.status.in_(terminal_states)
        )
        if team_user_ids is not None:
            run_query = run_query.where(Run.user_id.in_(team_user_ids))
        runs: list[Run] = list(session.execute(
            run_query.order_by(Run.created_at.desc())
        ).scalars())

        # Author rollup — join through users so the leaderboard
        # surfaces emails, not just user IDs.
        users = {
            u.id: u for u in session.execute(
                select(User).where(User.id.in_({r.user_id for r in runs}))
            ).scalars()
        }
        by_author: dict[int, dict] = defaultdict(
            lambda: {"runs": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        )
        for r in runs:
            agg = by_author[r.user_id]
            agg["runs"] += 1
            agg["cost_usd"] += r.cost_usd
            agg["input_tokens"] += r.input_tokens
            agg["output_tokens"] += r.output_tokens
        author_rows = []
        for uid, agg in by_author.items():
            u = users.get(uid)
            if u is None:
                # User row vanished (deleted but their runs kept).
                # Skip rather than render with `email=None` — the
                # leaderboard reading "Unknown spent $X" is worse
                # than the row not appearing.
                continue
            author_rows.append(AuthorRollup(
                user_id=uid,
                email=u.email,
                runs=agg["runs"],
                cost_usd=round(agg["cost_usd"], 6),
                input_tokens=agg["input_tokens"],
                output_tokens=agg["output_tokens"],
            ))
        author_rows.sort(key=lambda x: x.cost_usd, reverse=True)

        # Task rollup — what does this org spend most on?
        by_task: dict[str, dict] = defaultdict(lambda: {"runs": 0, "cost_usd": 0.0})
        for r in runs:
            agg = by_task[r.task]
            agg["runs"] += 1
            agg["cost_usd"] += r.cost_usd
        task_rows = [
            TaskRollup(task=t, runs=v["runs"], cost_usd=round(v["cost_usd"], 6))
            for t, v in by_task.items()
        ]
        task_rows.sort(key=lambda x: x.cost_usd, reverse=True)

        recent = [_run_to_row(r) for r in runs[:limit]]
        total_cost = sum(r.cost_usd for r in runs)

        return TeamDashboardResponse(
            org_id=org.id,
            org_slug=org.slug,
            total_runs=len(runs),
            total_cost_usd=round(total_cost, 6),
            by_author=author_rows,
            by_task=task_rows,
            recent=recent,
        )
