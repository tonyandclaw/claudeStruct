"""Tests for the production-readiness middleware additions:
CORS, X-Request-Id, and the deep /readyz check."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import (
    REQUEST_ID_HEADER,
    create_app,
    resolve_cors_origins,
)
from claudestruct.server.db import init_db


def _client(tmp_path, *, cors_origins=None):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    app = create_app(
        engine=engine, run_root=str(tmp_path), skip_init=True,
        cors_origins=cors_origins,
    )
    return TestClient(app), app


# --- resolve_cors_origins -----------------------------------------


def test_resolve_cors_origins_default_is_empty(monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_CORS_ORIGINS", raising=False)
    assert resolve_cors_origins() == []


def test_resolve_cors_origins_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_CORS_ORIGINS", "https://from-env.example")
    assert resolve_cors_origins(["https://from-arg.example"]) == [
        "https://from-arg.example",
    ]


def test_resolve_cors_origins_parses_csv_env(monkeypatch):
    monkeypatch.setenv(
        "CLAUDESTRUCT_CORS_ORIGINS",
        "https://app.example.com, https://other.example.com",
    )
    assert resolve_cors_origins() == [
        "https://app.example.com",
        "https://other.example.com",
    ]


# --- CORS middleware behaviour ------------------------------------


def test_cors_default_off_no_allow_origin_header(tmp_path):
    """Empty origins list → no CORSMiddleware mounted → no
    Access-Control-Allow-Origin header. Same-origin browsers don't
    notice; cross-origin SPAs are blocked, which is the safe
    default."""
    client, _ = _client(tmp_path, cors_origins=[])
    r = client.get(
        "/healthz",
        headers={"Origin": "https://random.example.com"},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {
        k.lower() for k in r.headers
    }


def test_cors_allowed_origin_gets_allow_origin_header(tmp_path):
    client, _ = _client(
        tmp_path, cors_origins=["https://app.example.com"],
    )
    r = client.get(
        "/healthz",
        headers={"Origin": "https://app.example.com"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_unallowed_origin_no_allow_origin_header(tmp_path):
    """An origin NOT in the allowlist must not get the
    `Access-Control-Allow-Origin: *` lazy fallback. The browser
    refuses to share the response."""
    client, _ = _client(
        tmp_path, cors_origins=["https://app.example.com"],
    )
    r = client.get(
        "/healthz",
        headers={"Origin": "https://attacker.example.com"},
    )
    # Server still serves the body (the policy is browser-side),
    # but the missing Allow-Origin header is what gates the read.
    assert "access-control-allow-origin" not in {
        k.lower() for k in r.headers
    }


def test_cors_preflight_returns_allow_methods(tmp_path):
    """OPTIONS preflight from an allowed origin must list the
    methods we whitelisted."""
    client, _ = _client(
        tmp_path, cors_origins=["https://app.example.com"],
    )
    r = client.options(
        "/v1/dashboard",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code in (200, 204)
    methods = r.headers.get("access-control-allow-methods", "")
    for m in ("GET", "POST", "DELETE"):
        assert m in methods


# --- X-Request-Id middleware --------------------------------------


def test_request_id_minted_when_absent(tmp_path):
    """No incoming X-Request-Id → server mints a fresh UUID and
    echoes it in the response."""
    client, _ = _client(tmp_path)
    r = client.get("/healthz")
    assert r.status_code == 200
    rid = r.headers.get(REQUEST_ID_HEADER)
    assert rid
    # UUID-ish — 32 hex chars + 4 dashes.
    assert len(rid) >= 32


def test_request_id_propagated_when_supplied(tmp_path):
    """A caller-supplied X-Request-Id (typical: a proxy or SPA
    chains correlation ids) is preserved on the response so a log
    correlator can stitch the trace."""
    client, _ = _client(tmp_path)
    r = client.get(
        "/healthz",
        headers={REQUEST_ID_HEADER: "test-correlation-id-abc"},
    )
    assert r.headers.get(REQUEST_ID_HEADER) == "test-correlation-id-abc"


def test_request_id_unique_per_request(tmp_path):
    """Two consecutive requests with no header get distinct ids
    (otherwise the correlation is meaningless)."""
    client, _ = _client(tmp_path)
    r1 = client.get("/healthz")
    r2 = client.get("/healthz")
    assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]


# --- /readyz deep check -------------------------------------------


def test_readyz_returns_200_when_db_responsive(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/readyz")
    assert r.status_code == 200


def test_readyz_returns_503_when_db_dies(tmp_path, monkeypatch):
    """Disposing the engine simulates a DB outage. /readyz must
    return 503 so k8s pulls the pod from service endpoints
    instead of letting every request 5xx."""
    client, app = _client(tmp_path)
    # Replace session_factory with one that raises on use.

    def _bad_factory():
        from sqlalchemy.exc import OperationalError
        raise OperationalError("simulated", {}, Exception("DB gone"))

    app.state.session_factory = _bad_factory
    r = client.get("/readyz")
    assert r.status_code == 503
    assert "db unavailable" in r.json()["detail"].lower()
