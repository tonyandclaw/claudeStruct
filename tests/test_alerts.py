"""Tests for cost-regression alerts (W6.5).

Covers:
- ``_mean_stddev`` math (empty / single / multi).
- ``compute_team_alerts`` baseline + flagging logic: per-task scoping,
  sample-size floor, recent window, threshold honored, failed-runs
  ignored, negative cost ignored, tenant isolation.
- ``GET /v1/alerts`` HTTP: auth gate, viewer 403, member 200, query-
  param validation, response shape, X-CS-Region header.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server import alerts as alerts_mod
from claudestruct.server.alerts import _mean_stddev, compute_team_alerts
from claudestruct.server.app import create_app
from claudestruct.server.auth import generate_key
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import (
    ApiKey,
    Membership,
    Org,
    Role,
    Run,
    RunStatus,
    User,
)

# --- _mean_stddev ----------------------------------------------------

def test_mean_stddev_empty_returns_zero_zero():
    assert _mean_stddev([]) == (0.0, 0.0)


def test_mean_stddev_single_sample_zero_stddev():
    mean, stddev = _mean_stddev([5.0])
    assert mean == 5.0
    assert stddev == 0.0


def test_mean_stddev_uses_sample_stddev_n_minus_1():
    """Hand-computed: samples=[1,2,3,4,5], mean=3, sum_sq=10,
    variance = 10/(5-1) = 2.5 → stddev ≈ 1.5811"""
    mean, stddev = _mean_stddev([1.0, 2.0, 3.0, 4.0, 5.0])
    assert mean == 3.0
    assert abs(stddev - 1.5811388300841898) < 1e-9


# --- compute_team_alerts ---------------------------------------------

def _seed_org_user(session, slug="acme", email="lead@acme.test"):
    org = Org(slug=slug, name=slug.upper())
    user = User(email=email)
    session.add_all([org, user])
    session.flush()
    session.add(Membership(user_id=user.id, org_id=org.id, role=Role.member.value))
    session.commit()
    return org.id, user.id


def _seed_run(
    session, *, org_id, user_id, run_id, task="dev",
    cost_usd=1.0, status=RunStatus.done.value, created_at=None,
):
    """Insert a Run row at a controlled created_at + cost. Bypasses
    the model defaults so we can place the row inside / outside the
    baseline + recent windows."""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    r = Run(
        run_id=run_id,
        org_id=org_id,
        user_id=user_id,
        status=status,
        task=task,
        description=run_id,
        cost_usd=cost_usd,
        created_at=created_at,
    )
    session.add(r)
    return r


@pytest.fixture()
def factory():
    """Per-test in-memory SQLite session factory.

    StaticPool keeps a single connection so the seed transaction here
    is visible to the request handlers. Mirrors test_server.py."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return make_session_factory(engine)


def test_no_runs_returns_empty_snapshot(factory):
    with factory() as session:
        org_id, _ = _seed_org_user(session)
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    assert snap.baselines == []


def test_below_min_sample_size_does_not_alert(factory):
    """Even an obvious outlier gets suppressed when the baseline has
    fewer than MIN_SAMPLE_SIZE samples — stddev is too noisy to trust
    on n=2."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        # 2 cheap baseline runs + 1 outlier; total 3 < MIN_SAMPLE_SIZE (5).
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-1", cost_usd=0.5, created_at=now - timedelta(days=10))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-2", cost_usd=0.6, created_at=now - timedelta(days=10))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-3", cost_usd=99.0, created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    # Baseline still surfaces, with stddev=None to signal "insufficient".
    assert len(snap.baselines) == 1
    assert snap.baselines[0].sample_size == 3
    assert snap.baselines[0].stddev_cost_usd is None


def test_steady_costs_produce_no_alerts(factory):
    """When every run is roughly the same cost, stddev is small but
    so is the deviation, so z stays well under threshold."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        for i in range(8):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=0.5 + 0.01 * i,
                      created_at=now - timedelta(hours=i))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    assert snap.baselines[0].sample_size == 8
    assert snap.baselines[0].stddev_cost_usd is not None


def test_clear_outlier_is_flagged(factory):
    """A run dramatically more expensive than the rest of its task's
    history fires an alert with z_score > threshold."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        # 6 calm baseline runs.
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=5, hours=i))
        # One outlier in the recent window.
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-spike", cost_usd=50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert len(snap.alerts) == 1
    a = snap.alerts[0]
    assert a.run_id == "r-spike"
    assert a.cost_usd == 50.0
    assert a.z_score > snap.sigma_threshold
    assert a.user_email == "lead@acme.test"


def test_per_task_baseline_does_not_cross_contaminate(factory):
    """A 5x ``plan`` shouldn't fire an alert just because ``review``
    runs are cheap. Each task is scored against its own baseline."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        # 6 cheap reviews — pulls overall cross-task average down.
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"rev-{i}", task="review", cost_usd=0.05,
                      created_at=now - timedelta(days=2, hours=i))
        # Plan runs at ~$5 with natural variance, plus a fresh run at
        # the baseline mean. Cross-task mean would put the plan at
        # ~100σ above review; per-task scoring keeps it within 1σ of
        # its own baseline → no alert.
        plan_costs = [4.5, 4.8, 5.0, 5.2, 5.0, 5.5]
        for i, cost in enumerate(plan_costs):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"plan-{i}", task="plan", cost_usd=cost,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="plan-fresh", task="plan", cost_usd=5.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    tasks = {b.task for b in snap.baselines}
    assert tasks == {"review", "plan"}


def test_old_outlier_outside_recent_window_not_flagged(factory):
    """An outlier that's already old (outside the recent window)
    isn't alerted — operators have presumably already seen it."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=10, hours=i))
        # Outlier 5 days ago — inside 30-day baseline, outside 72h
        # eval window.
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-old-spike", cost_usd=50.0,
                  created_at=now - timedelta(days=5))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    # ...but it does contribute to the baseline.
    plan_baseline = next(b for b in snap.baselines if b.task == "dev")
    assert plan_baseline.sample_size == 7


def test_failed_runs_excluded_from_baseline_and_alerts(factory):
    """Failed runs reflect partial work, not the operator's intent.
    Including them in the baseline would distort it (and a 30-second
    crash that happened to bill nothing would mask a real outlier)."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        # Failed run with absurd cost — must NOT be flagged.
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-failed-spike", cost_usd=200.0,
                  status=RunStatus.failed.value,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.alerts == []
    assert snap.baselines[0].sample_size == 6


def test_negative_cost_runs_skipped(factory):
    """Billing-correction rows or accidental negative writes shouldn't
    poison the baseline. The query filters them out at the SQL level."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        for i in range(5):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="r-neg", cost_usd=-50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert snap.baselines[0].sample_size == 5  # negative excluded
    assert snap.alerts == []


def test_tenant_isolation_alerts_scoped_to_caller_org(factory):
    """An outlier in org-b must not appear in org-a's alert feed."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        a_org, a_user = _seed_org_user(session, slug="org-a", email="a@a.test")
        b_org, b_user = _seed_org_user(session, slug="org-b", email="b@b.test")
        for i in range(6):
            _seed_run(session, org_id=a_org, user_id=a_user,
                      run_id=f"a-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        # Massive outlier inside org-b only.
        for i in range(6):
            _seed_run(session, org_id=b_org, user_id=b_user,
                      run_id=f"b-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=b_org, user_id=b_user,
                  run_id="b-spike", cost_usd=50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap_a = compute_team_alerts(session, org_id=a_org)
        snap_b = compute_team_alerts(session, org_id=b_org)
    assert snap_a.alerts == []
    assert len(snap_b.alerts) == 1
    assert snap_b.alerts[0].run_id == "b-spike"


def test_baseline_outside_lookback_not_used(factory):
    """A run from 60 days ago must not anchor a 30-day baseline."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        # Old high-cost runs outside the lookback — must NOT be loaded.
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"old-{i}", cost_usd=100.0,
                      created_at=now - timedelta(days=60, hours=i))
        # Fresh cheap runs inside the lookback.
        for i in range(6):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"new-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        # Recent outlier vs the recent baseline.
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="spike", cost_usd=50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id, lookback_days=30)
    assert snap.baselines[0].sample_size == 7  # new + spike, no old
    assert len(snap.alerts) == 1
    assert snap.alerts[0].run_id == "spike"


def test_alerts_sorted_by_z_score_descending(factory):
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        # 20 baseline + 2 outliers chosen so both clear 2σ but at
        # clearly different z-scores. With smaller baselines the
        # outliers themselves dominate the mean and the smaller one
        # gets dragged into "normal" — counterintuitive but accurate.
        for i in range(20):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="big", cost_usd=80.0,
                  created_at=now - timedelta(hours=2))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="medium", cost_usd=50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
        snap = compute_team_alerts(session, org_id=org_id)
    assert [a.run_id for a in snap.alerts] == ["big", "medium"]
    assert snap.alerts[0].z_score > snap.alerts[1].z_score


def test_custom_sigma_threshold_honored(factory):
    """A laxer threshold flags more, a stricter threshold flags fewer."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        org_id, user_id = _seed_org_user(session)
        for i in range(8):
            _seed_run(session, org_id=org_id, user_id=user_id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=org_id, user_id=user_id,
                  run_id="medium", cost_usd=2.5,
                  created_at=now - timedelta(hours=1))
        session.commit()
        # Strict: 5σ → no alert.
        strict = compute_team_alerts(session, org_id=org_id, sigma_threshold=5.0)
        assert strict.alerts == []
        # Lax: 0.5σ → flagged.
        lax = compute_team_alerts(session, org_id=org_id, sigma_threshold=0.5)
        assert any(a.run_id == "medium" for a in lax.alerts)


# --- HTTP endpoint --------------------------------------------------

@pytest.fixture()
def app_env(tmp_path):
    """Per-test app + a pre-seeded admin/member/viewer key set, mirrors
    test_server.py's `env` fixture style."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = make_session_factory(engine)
    keys: dict[str, str] = {}
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        session.add(org)
        session.flush()
        for email, role in [
            ("admin@acme", Role.admin),
            ("member@acme", Role.member),
            ("viewer@acme", Role.viewer),
        ]:
            user = User(email=email)
            session.add(user)
            session.flush()
            session.add(Membership(user_id=user.id, org_id=org.id, role=role.value))
            full, key_id, hashed = generate_key()
            session.add(ApiKey(
                user_id=user.id, org_id=org.id, key_id=key_id,
                hashed_secret=hashed, name=f"{email}-key",
            ))
            keys[email] = full
        session.commit()
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    return {
        "client": TestClient(app),
        "factory": factory,
        "keys": keys,
    }


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_get_alerts_requires_auth(app_env):
    r = app_env["client"].get("/v1/alerts")
    assert r.status_code == 401


def test_get_alerts_viewer_forbidden(app_env):
    r = app_env["client"].get(
        "/v1/alerts", headers=_auth(app_env["keys"]["viewer@acme"]),
    )
    assert r.status_code == 403


def test_get_alerts_member_ok_empty_org(app_env):
    r = app_env["client"].get(
        "/v1/alerts", headers=_auth(app_env["keys"]["member@acme"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"] == []
    assert body["baselines"] == []
    assert body["sigma_threshold"] == alerts_mod.DEFAULT_SIGMA_THRESHOLD
    assert body["lookback_days"] == alerts_mod.DEFAULT_LOOKBACK_DAYS


def test_get_alerts_response_shape(app_env):
    r = app_env["client"].get(
        "/v1/alerts", headers=_auth(app_env["keys"]["member@acme"]),
    )
    body = r.json()
    assert {
        "generated_at", "lookback_days", "recent_window_hours",
        "sigma_threshold", "min_sample_size", "baselines", "alerts",
    } <= set(body.keys())


def test_get_alerts_invalid_lookback_rejected(app_env):
    r = app_env["client"].get(
        "/v1/alerts?lookback_days=0",
        headers=_auth(app_env["keys"]["member@acme"]),
    )
    assert r.status_code == 422


def test_get_alerts_invalid_sigma_rejected(app_env):
    """sigma_threshold must be > 0; 0 / negatives are nonsensical
    (would flag every run including below-mean ones)."""
    r = app_env["client"].get(
        "/v1/alerts?sigma_threshold=0",
        headers=_auth(app_env["keys"]["member@acme"]),
    )
    assert r.status_code == 422


def test_get_alerts_endpoint_carries_x_cs_region(app_env):
    """W8.5 region middleware applies to alerts too."""
    r = app_env["client"].get(
        "/v1/alerts", headers=_auth(app_env["keys"]["member@acme"]),
    )
    assert "X-CS-Region" in r.headers


def test_get_alerts_returns_seeded_outlier(app_env):
    """End-to-end: seed the canonical 6-cheap + 1-spike pattern,
    verify the HTTP response carries the alert."""
    factory = app_env["factory"]
    now = datetime.now(timezone.utc)
    with factory() as session:
        # Look up the existing org/user from the fixture seed.
        from sqlalchemy import select
        org = session.execute(select(Org).where(Org.slug == "acme")).scalar_one()
        user = session.execute(
            select(User).where(User.email == "member@acme")
        ).scalar_one()
        for i in range(6):
            _seed_run(session, org_id=org.id, user_id=user.id,
                      run_id=f"r-{i}", cost_usd=1.0,
                      created_at=now - timedelta(days=2, hours=i))
        _seed_run(session, org_id=org.id, user_id=user.id,
                  run_id="r-spike", cost_usd=50.0,
                  created_at=now - timedelta(hours=1))
        session.commit()
    r = app_env["client"].get(
        "/v1/alerts", headers=_auth(app_env["keys"]["member@acme"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["alerts"]) == 1
    a = body["alerts"][0]
    assert a["run_id"] == "r-spike"
    assert a["task"] == "dev"
    assert a["user_email"] == "member@acme"
    assert a["cost_usd"] == 50.0
    assert a["z_score"] > body["sigma_threshold"]
