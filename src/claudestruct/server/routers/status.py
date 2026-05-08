"""Public read-only status page (A.6).

`GET /status` aggregates `/healthz` + DB connectivity + worker
queue depth + last successful run + recent fleet error rate into
a single document a status page can render.

Unauthenticated by design — same access model as ``/healthz`` and
``/v1/slo``. The payload is exclusively global aggregates and
static deployment facts (version, region); no tenant-specific
data leaks. A status-page widget can poll this every minute
without burning a bearer token.

Component states:
  * ``ok`` — operating normally
  * ``degraded`` — measurable problem but the surface is still
    serving (e.g. queue is deep but workers are draining)
  * ``down`` — the surface is unable to serve

Overall state is the worst of the components.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from claudestruct import __version__
from claudestruct.server.models import Run, RunStatus
from claudestruct.server.schema import ComponentStatus, StatusResponse

router = APIRouter(tags=["status"])


# Same thresholds the runbook references — keeping them as code-level
# constants so a regression that bumps "degraded" past "down" surfaces
# in review rather than ops.
QUEUE_DEPTH_DEGRADED = 50
QUEUE_DEPTH_DOWN = 500
ERROR_RATE_DEGRADED = 0.005   # 0.5% — twice the SLO target error rate
ERROR_RATE_DOWN = 0.05        # 5% — the surface is clearly unhealthy


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _check_db(session_factory) -> ComponentStatus:
    """Round-trip a trivial SELECT to confirm the DB pool is alive."""
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return ComponentStatus(name="db", state="ok")
    except SQLAlchemyError as exc:
        return ComponentStatus(
            name="db", state="down", detail=f"DB error: {type(exc).__name__}",
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(
            name="db", state="down", detail=f"unexpected: {type(exc).__name__}",
        )


def _check_worker(session_factory) -> tuple[ComponentStatus, int]:
    """Worker health is a function of queue depth: a deep queue
    means the worker isn't draining (or there's a runaway producer).
    Returns the queue depth alongside so the status response doesn't
    have to count rows twice."""
    try:
        with session_factory() as session:
            depth = session.execute(
                select(Run).where(Run.status == RunStatus.queued.value)
            ).scalars().all()
            queue_depth = len(depth)
    except Exception as exc:  # noqa: BLE001
        return (
            ComponentStatus(
                name="worker", state="down",
                detail=f"queue lookup failed: {type(exc).__name__}",
            ),
            -1,
        )
    if queue_depth >= QUEUE_DEPTH_DOWN:
        state = "down"
    elif queue_depth >= QUEUE_DEPTH_DEGRADED:
        state = "degraded"
    else:
        state = "ok"
    return (
        ComponentStatus(
            name="worker", state=state,
            detail=f"queue_depth={queue_depth}",
        ),
        queue_depth,
    )


def _last_successful_run_at(session_factory) -> datetime | None:
    try:
        with session_factory() as session:
            row = session.execute(
                select(Run).where(Run.status == RunStatus.done.value)
                .order_by(Run.ended_at.desc().nullslast()).limit(1)
            ).scalar_one_or_none()
            if row is None or row.ended_at is None:
                return None
            ts = row.ended_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
    except Exception:  # noqa: BLE001
        return None


def _recent_error_rate_1h(session_factory) -> float | None:
    """Fleet-wide error rate over the last hour. Returns None when
    there's not enough traffic to compute a meaningful rate."""
    try:
        with session_factory() as session:
            cutoff = _now_utc() - timedelta(hours=1)
            rows = list(session.execute(
                select(Run).where(
                    Run.status.in_(
                        (RunStatus.done.value, RunStatus.failed.value),
                    ),
                    Run.created_at >= cutoff,
                )
            ).scalars())
    except Exception:  # noqa: BLE001
        return None
    total = len(rows)
    if total < 10:
        # Not enough traffic — same floor as the burn-rate detector.
        return None
    failed = sum(1 for r in rows if r.status == RunStatus.failed.value)
    return failed / total


def _api_state_from_error_rate(rate: float | None) -> ComponentStatus:
    if rate is None:
        return ComponentStatus(
            name="api", state="ok",
            detail="insufficient traffic to estimate error rate",
        )
    if rate >= ERROR_RATE_DOWN:
        return ComponentStatus(
            name="api", state="down",
            detail=f"recent error rate {rate:.2%} ≥ {ERROR_RATE_DOWN:.0%}",
        )
    if rate >= ERROR_RATE_DEGRADED:
        return ComponentStatus(
            name="api", state="degraded",
            detail=f"recent error rate {rate:.2%}",
        )
    return ComponentStatus(
        name="api", state="ok",
        detail=f"recent error rate {rate:.2%}",
    )


def _worst(states: list[str]) -> str:
    """Compose component states into a single overall state.

    A component-level ``down`` propagates to overall ``down``;
    otherwise ``degraded`` propagates; otherwise ``ok``.
    """
    if "down" in states:
        return "down"
    if "degraded" in states:
        return "degraded"
    return "ok"


@router.get("/status", response_model=StatusResponse)
def get_status(request: Request) -> StatusResponse:
    """Public status snapshot.

    Unauthenticated by design — the payload is global aggregates,
    no tenant-specific data. A status page can poll without a
    bearer token.
    """
    factory = request.app.state.session_factory
    region = getattr(request.app.state, "region", None)

    db = _check_db(factory)
    worker, queue_depth = _check_worker(factory)
    error_rate = _recent_error_rate_1h(factory)
    api = _api_state_from_error_rate(error_rate)
    last_successful = _last_successful_run_at(factory)

    components = [db, worker, api]
    overall = _worst([c.state for c in components])
    return StatusResponse(
        version=__version__,
        region=region,
        overall=overall,
        components=components,
        queue_depth=max(queue_depth, 0),
        last_successful_run_at=last_successful,
        recent_error_rate_1h=error_rate,
        generated_at=_now_utc(),
    )
