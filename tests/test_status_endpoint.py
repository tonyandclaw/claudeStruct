"""Tests for the public status endpoint (A.6)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import create_app
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import Org, Run, RunStatus, User


@pytest.fixture()
def status_env(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        user = User(email="alice@acme")
        session.add_all([org, user])
        session.commit()
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    return {
        "client": TestClient(app),
        "factory": factory,
        "engine": engine,
    }


def _seed_runs(env, *, total: int, failed: int, status_kind: str = "terminal"):
    """Seed runs for the rate calculation. `status_kind` picks done /
    failed / queued; `failed` controls the failed-vs-done split when
    `status_kind=terminal`."""
    with env["factory"]() as session:
        org = session.execute(
            __import__("sqlalchemy").select(Org).where(Org.slug == "acme")
        ).scalar_one()
        user = session.execute(
            __import__("sqlalchemy").select(User).where(User.email == "alice@acme")
        ).scalar_one()
        for i in range(total):
            if status_kind == "queued":
                status = RunStatus.queued.value
            else:
                status = (
                    RunStatus.failed.value if i < failed
                    else RunStatus.done.value
                )
            session.add(Run(
                run_id=f"run-{status_kind}-{i}",
                org_id=org.id, user_id=user.id,
                status=status, task="dev", description="x",
                cost_usd=0.1,
                # `done` runs need ended_at populated for
                # last_successful_run_at to find them.
                ended_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
                if status == RunStatus.done.value else None,
            ))
        session.commit()


# --- Route shape -----------------------------------------------------


def test_status_endpoint_unauthenticated(status_env):
    """Status page must be reachable without a bearer token —
    that's the whole point: external monitoring can scrape it."""
    r = status_env["client"].get("/status")
    assert r.status_code == 200


def test_status_endpoint_response_shape(status_env):
    r = status_env["client"].get("/status").json()
    assert "version" in r
    assert "overall" in r
    assert r["overall"] in {"ok", "degraded", "down"}
    assert isinstance(r["components"], list)
    component_names = {c["name"] for c in r["components"]}
    assert {"db", "worker", "api"} <= component_names
    assert r["queue_depth"] == 0
    # last_successful_run_at + recent_error_rate_1h are nullable on
    # an empty fleet — the field must exist regardless.
    assert "last_successful_run_at" in r
    assert "recent_error_rate_1h" in r


def test_status_endpoint_no_tenant_data_leaks(status_env):
    """No org slugs, run ids, user emails should appear anywhere in
    the public payload — only global aggregates."""
    body = status_env["client"].get("/status").text
    assert "acme" not in body.lower()
    assert "alice@acme" not in body.lower()
    assert "run-" not in body  # no run_id leaks


# --- State derivation ------------------------------------------------


def test_status_overall_ok_on_empty_fleet(status_env):
    body = status_env["client"].get("/status").json()
    assert body["overall"] == "ok"


def test_status_worker_degraded_when_queue_deep(status_env):
    """Queue depth ≥ 50 → worker degraded; overall = degraded."""
    _seed_runs(status_env, total=60, failed=0, status_kind="queued")
    body = status_env["client"].get("/status").json()
    assert body["queue_depth"] == 60
    worker = next(c for c in body["components"] if c["name"] == "worker")
    assert worker["state"] == "degraded"
    assert body["overall"] == "degraded"


def test_status_worker_down_when_queue_huge(status_env):
    """Queue depth ≥ 500 → worker down; overall = down."""
    _seed_runs(status_env, total=600, failed=0, status_kind="queued")
    body = status_env["client"].get("/status").json()
    worker = next(c for c in body["components"] if c["name"] == "worker")
    assert worker["state"] == "down"
    assert body["overall"] == "down"


def test_status_api_degraded_when_recent_error_rate_high(status_env):
    """30 terminal runs with 3 failed (10% error) → api down (≥5%)."""
    _seed_runs(status_env, total=30, failed=3, status_kind="terminal")
    body = status_env["client"].get("/status").json()
    api = next(c for c in body["components"] if c["name"] == "api")
    assert api["state"] in {"degraded", "down"}
    assert body["recent_error_rate_1h"] is not None
    assert body["recent_error_rate_1h"] == pytest.approx(3 / 30)


def test_status_api_ok_at_low_traffic(status_env):
    """Below the 10-run floor the rate is None and api stays ok —
    a single failure must not page anyone."""
    _seed_runs(status_env, total=3, failed=2, status_kind="terminal")
    body = status_env["client"].get("/status").json()
    api = next(c for c in body["components"] if c["name"] == "api")
    assert api["state"] == "ok"
    assert body["recent_error_rate_1h"] is None


def test_status_last_successful_run_at_populated(status_env):
    _seed_runs(status_env, total=5, failed=0, status_kind="terminal")
    body = status_env["client"].get("/status").json()
    assert body["last_successful_run_at"] is not None
