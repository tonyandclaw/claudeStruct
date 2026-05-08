"""FastAPI app factory.

``create_app(...)`` is the single seam for tests and the ``cs serve``
launcher. It builds the engine + session factory, wires up
``app.state``, and mounts the routers. The OpenAPI doc is auto-generated
at ``/openapi.json`` (FastAPI default); ``/docs`` and ``/redoc`` serve
the interactive viewer.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from claudestruct import __version__
from claudestruct.server.db import init_db, make_engine, make_session_factory
from claudestruct.server.latency import LatencyTracker, RequestLatencyMiddleware
from claudestruct.server.routers import (
    audit as audit_router,
)
from claudestruct.server.routers import (
    billing as billing_router,
)
from claudestruct.server.routers import (
    budget as budget_router,
)
from claudestruct.server.routers import (
    dashboard as dashboard_router,
)
from claudestruct.server.routers import (
    github as github_router,
)
from claudestruct.server.routers import (
    health as health_router,
)
from claudestruct.server.routers import (
    keys as keys_router,
)
from claudestruct.server.routers import (
    oauth as oauth_router,
)
from claudestruct.server.routers import (
    runs as runs_router,
)
from claudestruct.server.routers import (
    slo as slo_router,
)

# --- W8.5 -----------------------------------------------------------

DEFAULT_REGION = "us-east-1"


def resolve_region(override: str | None = None) -> str:
    """Pick the region tag. Priority: explicit override > env > default.

    The tag is provable on every response via the ``X-CS-Region``
    header so a client can verify their request didn't cross a
    residency boundary mid-flight (e.g. mistakenly hit the US shard
    when their org is pinned to EU). Treat it as advisory metadata --
    real residency enforcement happens at the load-balancer / DNS
    layer (per-region deployments + tenant region pin).
    """
    if override:
        return override
    return os.environ.get("CLAUDESTRUCT_REGION", DEFAULT_REGION)


class RegionHeaderMiddleware(BaseHTTPMiddleware):
    """Stamp every response with ``X-CS-Region`` so clients can
    verify they reached the expected residency shard."""

    def __init__(self, app: Any, region: str) -> None:
        super().__init__(app)
        self.region = region

    async def dispatch(self, request: Request, call_next):  # noqa: D401
        response = await call_next(request)
        response.headers["X-CS-Region"] = self.region
        return response


# A.4 — API version + deprecation policy. Routes already live under
# ``/v1/...``; the header is the second proof so a client built
# against the docs can sanity-check that they're talking to the
# version they expect (catches a misconfigured proxy that stripped
# the path prefix). Bumps to ``v2`` when the next major contract
# lands; ``v1`` will then carry ``Sunset:`` per docs/api-versioning.md.
API_VERSION = "v1"


class ApiVersionHeaderMiddleware(BaseHTTPMiddleware):
    """Stamp every response with ``X-CS-Api-Version`` so clients can
    detect a version mismatch without parsing the URL."""

    def __init__(self, app: Any, version: str) -> None:
        super().__init__(app)
        self.version = version

    async def dispatch(self, request: Request, call_next):  # noqa: D401
        response = await call_next(request)
        response.headers["X-CS-Api-Version"] = self.version
        return response


def create_app(
    *,
    db_url: str | None = None,
    run_root: str | Path | None = None,
    engine: Any = None,
    skip_init: bool = False,
    region: str | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        db_url: SQLAlchemy URL. Defaults to env ``CLAUDESTRUCT_DATABASE_URL``
            or ``sqlite:///./.claudestruct/server.db``.
        run_root: Directory whose ``.claudestruct/runs/`` is read by the
            dashboard / budget / runs endpoints. Defaults to cwd.
        engine: Pre-built SQLAlchemy engine (tests use this with an
            in-memory SQLite to avoid touching the filesystem).
        skip_init: Skip the ``Base.metadata.create_all`` call. Useful
            when the engine is shared across test app instances and
            tables already exist.
        region: W8.5 data-residency tag (e.g. ``us-east-1``,
            ``eu-west-1``). Surfaced in ``/healthz`` + on every
            response via ``X-CS-Region``. Defaults to env
            ``CLAUDESTRUCT_REGION`` or ``us-east-1``.
    """
    eng = engine if engine is not None else make_engine(db_url)
    if not skip_init:
        init_db(eng)

    app = FastAPI(
        title="claudeStruct",
        version=__version__,
        description=(
            "REST API for the claudeStruct daemon. Authentication is "
            "Bearer-token (`ck_<key_id>_<secret>`). RBAC roles: admin, "
            "member, viewer. See `cs serve --help` for setup."
        ),
    )
    app.state.session_factory = make_session_factory(eng)
    app.state.engine = eng
    app.state.run_root = str(run_root) if run_root else "."
    app.state.region = resolve_region(region)
    app.state.latency_tracker = LatencyTracker()
    app.add_middleware(RequestLatencyMiddleware, tracker=app.state.latency_tracker)
    app.add_middleware(RegionHeaderMiddleware, region=app.state.region)
    app.add_middleware(ApiVersionHeaderMiddleware, version=API_VERSION)

    app.include_router(health_router.router)
    app.include_router(keys_router.router)
    app.include_router(dashboard_router.router)
    app.include_router(budget_router.router)
    app.include_router(runs_router.router)
    app.include_router(audit_router.router)
    app.include_router(billing_router.router)
    app.include_router(github_router.router)
    app.include_router(oauth_router.router)
    app.include_router(slo_router.router)

    return app
