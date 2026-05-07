"""Public SLO endpoint (W8.7).

GET /v1/slo — unauthenticated fleet-wide snapshot for the external
status page.

GET /v1/slo/tenant — authenticated per-org snapshot (viewer+) scoped
to the caller's org.

GET /v1/slo/latency — unauthenticated Prometheus text exposure of
per-route HTTP request latency percentiles (p50/p95/p99) collected by
RequestLatencyMiddleware. Designed for scraping by Prometheus or
node_exporter's textfile collector.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from claudestruct.server import auth as auth_mod
from claudestruct.server import billing as billing_mod
from claudestruct.server import slo as slo_mod
from claudestruct.server.latency import LatencyTracker
from claudestruct.server.models import Org, Role
from claudestruct.server.schema import (
    SloSnapshotResponse,
    SloTargetsResponse,
    SloWindow,
    TenantSloResponse,
)

router = APIRouter(tags=["slo"])


@router.get("/v1/slo", response_model=SloSnapshotResponse)
def get_slo(request: Request) -> SloSnapshotResponse:
    factory = request.app.state.session_factory
    with factory() as session:
        snap = slo_mod.compute_snapshot(session)
    return SloSnapshotResponse(
        generated_at=snap.generated_at,
        targets=SloTargetsResponse(
            success_rate=snap.targets.success_rate,
            p95_run_start_ms=snap.targets.p95_run_start_ms,
            p95_duration_ms=snap.targets.p95_duration_ms,
        ),
        windows=[
            SloWindow(
                window=w.window,  # type: ignore[arg-type]
                total_runs=w.total_runs,
                succeeded=w.succeeded,
                failed=w.failed,
                success_rate=w.success_rate,
                error_rate=w.error_rate,
                p50_run_start_ms=w.p50_run_start_ms,
                p95_run_start_ms=w.p95_run_start_ms,
                p99_run_start_ms=w.p99_run_start_ms,
                p50_duration_ms=w.p50_duration_ms,
                p95_duration_ms=w.p95_duration_ms,
                p99_duration_ms=w.p99_duration_ms,
            )
            for w in snap.windows
        ],
    )


@router.get("/v1/slo/tenant", response_model=TenantSloResponse)
def get_tenant_slo(
    request: Request,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
) -> TenantSloResponse:
    """Per-org SLO snapshot scoped to the caller's org (W8.7)."""
    factory = request.app.state.session_factory
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.id == principal.org_id)
        ).scalar_one_or_none()
        if org is None:
            raise HTTPException(status_code=404, detail="org not found")

        sub = billing_mod.get_or_default(session, org.id)
        snap = slo_mod.compute_tenant_snapshot(session, org.id)

    return TenantSloResponse(
        generated_at=snap.generated_at,
        targets=SloTargetsResponse(
            success_rate=snap.targets.success_rate,
            p95_run_start_ms=snap.targets.p95_run_start_ms,
            p95_duration_ms=snap.targets.p95_duration_ms,
        ),
        windows=[
            SloWindow(
                window=w.window,  # type: ignore[arg-type]
                total_runs=w.total_runs,
                succeeded=w.succeeded,
                failed=w.failed,
                success_rate=w.success_rate,
                error_rate=w.error_rate,
                p50_run_start_ms=w.p50_run_start_ms,
                p95_run_start_ms=w.p95_run_start_ms,
                p99_run_start_ms=w.p99_run_start_ms,
                p50_duration_ms=w.p50_duration_ms,
                p95_duration_ms=w.p95_duration_ms,
                p99_duration_ms=w.p99_duration_ms,
            )
            for w in snap.windows
        ],
        org_slug=org.slug,
        tier=sub.tier,
    )


@router.get("/v1/slo/latency", response_class=PlainTextResponse)
def get_slo_latency(request: Request) -> str:
    """Prometheus text exposure of per-route HTTP latency percentiles.

    Unauthenticated — designed to be scraped by a Prometheus server or
    node_exporter textfile collector on the same private network. The
    endpoint returns no sensitive data (only aggregate timings per
    route path).
    """
    tracker: LatencyTracker = request.app.state.latency_tracker
    return tracker.render_prometheus()
