"""Tests for the X-CS-Api-Version header (A.4)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import API_VERSION, create_app
from claudestruct.server.db import init_db


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    return TestClient(app)


def test_api_version_header_present_on_health(client):
    """Every response (including unauthenticated ones like /healthz)
    carries `X-CS-Api-Version`. A misconfigured proxy that stripped
    the path prefix would still fail this check."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("X-CS-Api-Version") == "v1"


def test_api_version_header_present_on_404(client):
    """The header lives in middleware, so it covers FastAPI's 404
    response too — clients can sanity-check the version even when
    they hit a typo'd path."""
    r = client.get("/no-such-endpoint")
    assert r.status_code == 404
    assert r.headers.get("X-CS-Api-Version") == "v1"


def test_api_version_constant_matches_path_prefix():
    """If `API_VERSION` ever drifts from the `/v1` URL prefix used
    in the routers, our docs (api-versioning.md) become a lie.
    Lock it."""
    assert API_VERSION == "v1"


def test_region_and_api_version_headers_coexist(client):
    """Both response-stamping middlewares must run — losing either
    breaks a documented client invariant."""
    r = client.get("/healthz")
    assert r.headers.get("X-CS-Region") is not None
    assert r.headers.get("X-CS-Api-Version") == "v1"
