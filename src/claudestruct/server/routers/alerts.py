"""Cost-regression alerts endpoint (W6.5).

``GET /v1/alerts`` surfaces this org's recent runs whose cost is more
than ``sigma_threshold`` standard deviations above the per-task
baseline. Member+ can read; the team-dashboard SPA renders these next
to the leaderboard so a lead can spot a runaway ``cs plan`` without
manually scrolling.

The endpoint is intentionally read-only and stateless. Notification
delivery (Slack / email) is a separate surface still pending under the
W6.5 entry — this endpoint is what those deliverers will poll once
they're built.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from claudestruct.server import alerts as alerts_mod
from claudestruct.server import auth as auth_mod
from claudestruct.server.models import Role
from claudestruct.server.schema import (
    RunAlertResponse,
    TaskBaselineResponse,
    TeamAlertsResponse,
)

router = APIRouter(prefix="/v1", tags=["alerts"])


@router.get("/alerts", response_model=TeamAlertsResponse)
def get_team_alerts(
    request: Request,
    lookback_days: int = Query(
        default=alerts_mod.DEFAULT_LOOKBACK_DAYS, ge=1, le=365,
        description="Days of history that build the per-task baseline.",
    ),
    recent_window_hours: int = Query(
        default=alerts_mod.DEFAULT_RECENT_WINDOW_HOURS, ge=1, le=24 * 30,
        description="Only runs created within this many hours can fire alerts.",
    ),
    sigma_threshold: float = Query(
        default=alerts_mod.DEFAULT_SIGMA_THRESHOLD, gt=0, le=10,
        description="Z-score threshold above which a run is flagged.",
    ),
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.member)),
) -> TeamAlertsResponse:
    # Defence-in-depth against query-string nonsense the FastAPI bounds
    # don't catch (e.g. NaN sneaking in via a manually-crafted client).
    if sigma_threshold != sigma_threshold:  # NaN check
        raise HTTPException(status_code=400, detail="sigma_threshold must be a real number")

    factory = request.app.state.session_factory
    with factory() as session:
        snap = alerts_mod.compute_team_alerts(
            session,
            org_id=principal.org_id,
            lookback_days=lookback_days,
            recent_window_hours=recent_window_hours,
            sigma_threshold=sigma_threshold,
        )

    return TeamAlertsResponse(
        generated_at=snap.generated_at,
        lookback_days=snap.lookback_days,
        recent_window_hours=snap.recent_window_hours,
        sigma_threshold=snap.sigma_threshold,
        min_sample_size=snap.min_sample_size,
        baselines=[
            TaskBaselineResponse(
                task=b.task,
                sample_size=b.sample_size,
                mean_cost_usd=b.mean_cost_usd,
                stddev_cost_usd=b.stddev_cost_usd,
                max_cost_usd=b.max_cost_usd,
            )
            for b in snap.baselines
        ],
        alerts=[
            RunAlertResponse(
                run_id=a.run_id,
                task=a.task,
                user_id=a.user_id,
                user_email=a.user_email,
                cost_usd=a.cost_usd,
                baseline_mean_usd=a.baseline_mean_usd,
                baseline_stddev_usd=a.baseline_stddev_usd,
                z_score=a.z_score,
                threshold_sigma=a.threshold_sigma,
                created_at=a.created_at,
            )
            for a in snap.alerts
        ],
    )
