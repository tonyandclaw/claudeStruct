"""Health probes — unauthenticated, suitable for k8s liveness/readiness.

* ``/healthz`` is the **liveness** probe — answers "is the process
  alive?" Cheap and dependency-free; k8s should restart the pod if
  it goes red.
* ``/readyz`` is the **readiness** probe — answers "should this pod
  receive traffic?" Returns 503 when a downstream dependency the
  request handlers need is unavailable, which causes k8s to remove
  the pod from the service endpoints until it comes back. The DB
  ping is the canonical dependency to check; without it the pod
  would 5xx every request anyway.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from claudestruct import __version__
from claudestruct.server.schema import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse, tags=["health"])
def healthz(request: Request) -> HealthResponse:
    region = getattr(request.app.state, "region", None)
    return HealthResponse(version=__version__, region=region)


@router.get("/readyz", response_model=HealthResponse, tags=["health"])
def readyz(request: Request) -> HealthResponse:
    """Deep readiness check.

    Round-trips a trivial ``SELECT 1`` against the configured DB
    pool. If the DB is gone — pool exhausted, network partition,
    server stopped — we return 503 so k8s pulls this pod out of
    the service endpoints until it recovers, rather than letting
    every request 5xx its way through to the user.
    """
    factory = getattr(request.app.state, "session_factory", None)
    region = getattr(request.app.state, "region", None)
    if factory is None:
        # Tests build the app without a session factory in some
        # paths — a dependency-less readiness check is still
        # better than crashing.
        return HealthResponse(version=__version__, region=region)
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"db unavailable: {type(exc).__name__}",
        ) from exc
    return HealthResponse(version=__version__, region=region)
