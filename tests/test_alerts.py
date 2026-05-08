"""Tests for the notification surface + cost-regression detector (W6.5)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server import alerts as alerts_mod
from claudestruct.server import notify as notify_mod
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import Org, Run, RunStatus, User

# --- Fixtures -------------------------------------------------------


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture()
def env(factory):
    """Two orgs (acme, beta) each with one user, ready for run seeds."""
    with factory() as session:
        acme = Org(slug="acme", name="Acme")
        beta = Org(slug="beta", name="Beta")
        ua = User(email="root@acme.test")
        ub = User(email="root@beta.test")
        session.add_all([acme, beta, ua, ub])
        session.flush()
        ids = {
            "acme_id": acme.id,
            "beta_id": beta.id,
            "ua_id": ua.id,
            "ub_id": ub.id,
        }
        session.commit()
    return {"factory": factory, **ids}


def _seed_run(
    session,
    *,
    org_id: int,
    user_id: int,
    run_id: str,
    cost: float,
    status: str = RunStatus.done.value,
    created_at: datetime,
) -> Run:
    r = Run(
        run_id=run_id,
        org_id=org_id,
        user_id=user_id,
        status=status,
        task="dev",
        description="x",
        cost_usd=cost,
        created_at=created_at,
    )
    session.add(r)
    return r


class _CapturingNotifier:
    name = "capture"

    def __init__(self) -> None:
        self.calls: list[notify_mod.Alert] = []

    def notify(self, alert: notify_mod.Alert) -> None:
        self.calls.append(alert)


# --- _stddev / sanity ----------------------------------------------


def test_stddev_empty_returns_zero():
    assert alerts_mod._stddev([], 0.0) == 0.0


def test_stddev_single_returns_zero():
    assert alerts_mod._stddev([5.0], 5.0) == 0.0


def test_stddev_population_formula():
    # population stddev of [1, 2, 3] with mean 2 is sqrt((1+0+1)/3)
    assert alerts_mod._stddev([1.0, 2.0, 3.0], 2.0) == pytest.approx(
        math.sqrt(2 / 3),
    )


# --- compute_cost_regression_alerts ---------------------------------


def test_no_alerts_when_org_has_too_few_runs(env):
    """Orgs with < 3 runs in window have no statistical baseline; skip."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(2):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-{i}", cost=1.0,
                created_at=now - timedelta(hours=1),
            )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert findings == []


def test_no_alerts_when_all_runs_same_cost(env):
    """Stddev=0 baseline; no run is "above" the mean."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(5):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-{i}", cost=1.0,
                created_at=now - timedelta(hours=1),
            )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert findings == []


def test_run_below_mean_is_not_flagged(env):
    """Cheap runs aren't regressions."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        # 3 runs at $10, 1 recent run at $1 — no alert.
        for i in range(3):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-base-{i}", cost=10.0,
                created_at=now - timedelta(days=5),
            )
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-cheap", cost=1.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert findings == []


def test_recent_run_above_threshold_is_flagged(env):
    """Cost > 2σ above mean → flagged."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        # Baseline: 10 runs at $1; recent: 1 at $10.
        for i in range(10):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-base-{i}", cost=1.0,
                created_at=now - timedelta(days=10),
            )
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-spike", cost=10.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(
            session, now=now, sigma=2.0,
        )
    assert len(findings) == 1
    f = findings[0]
    assert f.run_id == "r-spike"
    assert f.org_slug == "acme"
    assert f.run_cost_usd == 10.0
    assert f.sigma_above >= 2.0


def test_old_runs_not_re_alerted_when_outside_recent_window(env):
    """A spike that's older than ``check_recent_hours`` shouldn't fire
    every cron tick. It still contributes to the baseline."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(5):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-base-{i}", cost=1.0,
                created_at=now - timedelta(days=5),
            )
        # Spike from a week ago — outside default 24h recent window.
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-old-spike", cost=20.0,
            created_at=now - timedelta(days=7),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert findings == []


def test_failed_runs_excluded_from_baseline_and_alerts(env):
    """Failed runs' cost reflects partial work; not comparable."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(5):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-base-{i}", cost=1.0,
                created_at=now - timedelta(days=5),
            )
        # Big-cost recent FAILED run — must not fire (failed excluded).
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-bad", cost=99.0,
            status=RunStatus.failed.value,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert findings == []


def test_alerts_are_per_org(env):
    """A spike in acme must NOT show up under beta's slug."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(5):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"a-{i}", cost=1.0,
                created_at=now - timedelta(days=2),
            )
            _seed_run(
                session,
                org_id=env["beta_id"], user_id=env["ub_id"],
                run_id=f"b-{i}", cost=1.0,
                created_at=now - timedelta(days=2),
            )
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="a-spike", cost=10.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    assert len(findings) == 1
    assert findings[0].org_slug == "acme"


def test_findings_sorted_by_sigma_descending(env):
    """The worst regression appears first — operator-friendly default.

    Note: a single huge outlier inflates stddev enough that smaller
    outliers may not exceed the default 2σ threshold. We lower sigma
    here so both outliers fire and we can assert sort order.
    """
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(20):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-base-{i}", cost=1.0,
                created_at=now - timedelta(days=2),
            )
        # Two recent outliers — both should fire at sigma=0.5.
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-bigger", cost=10.0,
            created_at=now - timedelta(hours=2),
        )
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-smaller", cost=5.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(
            session, now=now, sigma=0.5,
        )
    assert [f.run_id for f in findings] == ["r-bigger", "r-smaller"]


# --- finding_to_alert severity ladder ------------------------------


def _finding(sigma_above: float) -> alerts_mod.CostRegressionFinding:
    return alerts_mod.CostRegressionFinding(
        org_id=1, org_slug="acme", run_id="r1",
        run_cost_usd=10.0, baseline_mean=1.0,
        baseline_stddev=1.0, sigma_above=sigma_above,
    )


def test_finding_to_alert_info_severity_at_2_sigma():
    a = alerts_mod.finding_to_alert(_finding(2.0))
    assert a.severity == "info"


def test_finding_to_alert_warning_at_3_sigma():
    a = alerts_mod.finding_to_alert(_finding(3.0))
    assert a.severity == "warning"


def test_finding_to_alert_critical_at_4_sigma():
    a = alerts_mod.finding_to_alert(_finding(4.0))
    assert a.severity == "critical"


def test_finding_to_alert_critical_for_inf_sigma():
    """Stddev=0 baseline reports inf σ; must not crash the formatter."""
    a = alerts_mod.finding_to_alert(_finding(float("inf")))
    assert a.severity == "critical"
    assert "∞" in a.summary
    assert a.details["sigma_above"] == "inf"


# --- dispatch_findings + Notifier integration -----------------------


def test_dispatch_findings_calls_notifier_for_each(env):
    """End-to-end: producer → dispatch → CapturingNotifier counts up."""
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        for i in range(10):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"r-{i}", cost=1.0,
                created_at=now - timedelta(days=2),
            )
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="r-spike", cost=10.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
        findings = alerts_mod.compute_cost_regression_alerts(session, now=now)
    notifier = _CapturingNotifier()
    n = alerts_mod.dispatch_findings(findings, notifier)
    assert n == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0].kind == "cost_regression"
    assert notifier.calls[0].org_slug == "acme"


# --- LogNotifier ----------------------------------------------------


def test_log_notifier_writes_warning_for_warning_severity(caplog):
    notifier = notify_mod.LogNotifier()
    alert = notify_mod.Alert(
        kind="cost_regression", severity="warning",
        org_slug="acme", summary="x", details={},
    )
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(alert)
    assert any("alert" in r.message for r in caplog.records)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_log_notifier_writes_error_for_critical_severity(caplog):
    notifier = notify_mod.LogNotifier()
    alert = notify_mod.Alert(
        kind="cost_regression", severity="critical",
        org_slug="acme", summary="x", details={},
    )
    with caplog.at_level("ERROR", logger="claudestruct.notify"):
        notifier.notify(alert)
    assert any(r.levelname == "ERROR" for r in caplog.records)


# --- SlackWebhookNotifier ------------------------------------------


class _FakeHttpClient:
    """Captures POST calls for SlackWebhookNotifier tests."""

    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._status_code = status_code

    def post(self, url, **kw):
        self.calls.append((url, kw))
        class _R:
            pass
        _R.status_code = self._status_code
        return _R


def test_slack_notifier_rejects_empty_url():
    with pytest.raises(ValueError, match="non-empty"):
        notify_mod.SlackWebhookNotifier("")


def test_slack_notifier_posts_to_webhook_url():
    client = _FakeHttpClient()
    notifier = notify_mod.SlackWebhookNotifier(
        "https://hooks.slack.com/services/X/Y/Z",
        http_client=client,
    )
    alert = notify_mod.Alert(
        kind="cost_regression", severity="warning",
        org_slug="acme", summary="run cost spike",
        details={"run_id": "r1", "sigma_above": 3.5},
    )
    notifier.notify(alert)
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == "https://hooks.slack.com/services/X/Y/Z"
    payload = kwargs["json"]
    assert "WARNING" in payload["text"]
    assert "acme" in payload["text"]
    # Slack attachments fields include the alert details.
    field_titles = {f["title"] for f in payload["attachments"][0]["fields"]}
    assert "kind" in field_titles
    assert "run_id" in field_titles


def test_slack_notifier_swallows_non_200(caplog):
    """Slack delivery failures must not crash the cron-driven caller."""
    client = _FakeHttpClient(status_code=502)
    notifier = notify_mod.SlackWebhookNotifier(
        "https://hooks.slack.com/X", http_client=client,
    )
    alert = notify_mod.Alert(
        kind="x", severity="info", org_slug="acme",
        summary="s", details={},
    )
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(alert)  # must not raise
    assert any("502" in r.message for r in caplog.records)


# --- default_notifier factory --------------------------------------


def test_default_notifier_returns_log_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_NOTIFY_PROVIDER", raising=False)
    n = notify_mod.default_notifier()
    assert n.name == "log"


def test_default_notifier_returns_slack_when_configured(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "slack")
    monkeypatch.setenv("CLAUDESTRUCT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/X")
    n = notify_mod.default_notifier()
    assert n.name == "slack"


def test_default_notifier_slack_without_url_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "slack")
    monkeypatch.delenv("CLAUDESTRUCT_SLACK_WEBHOOK_URL", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDESTRUCT_SLACK_WEBHOOK_URL"):
        notify_mod.default_notifier()


def test_default_notifier_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="unknown"):
        notify_mod.default_notifier()


# --- EmailNotifier --------------------------------------------------


class _FakeSMTP:
    """Records calls; mimics the subset of smtplib.SMTP that
    EmailNotifier exercises."""

    def __init__(
        self,
        *,
        starttls_raises: bool = False,
        login_raises: bool = False,
        sendmail_raises: bool = False,
    ) -> None:
        self.starttls_raises = starttls_raises
        self.login_raises = login_raises
        self.sendmail_raises = sendmail_raises
        self.calls: list[tuple[str, tuple, dict]] = []
        self.starttls_called = False
        self.login_args: tuple | None = None
        self.sendmail_args: tuple | None = None
        self.quit_called = False

    def starttls(self):
        self.starttls_called = True
        if self.starttls_raises:
            raise RuntimeError("STARTTLS not supported")

    def login(self, username, password):
        self.login_args = (username, password)
        if self.login_raises:
            raise RuntimeError("login failed")

    def sendmail(self, from_addr, to_addrs, msg):
        self.sendmail_args = (from_addr, to_addrs, msg)
        if self.sendmail_raises:
            raise RuntimeError("sendmail failed")

    def quit(self):
        self.quit_called = True


def _make_email_notifier(
    smtp: _FakeSMTP,
    *,
    username: str = "alerts@example.com",
    to_addrs: list[str] | None = None,
):
    return notify_mod.EmailNotifier(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username=username,
        password="hunter2",
        from_addr="alerts@example.com",
        to_addrs=to_addrs or ["oncall@example.com"],
        smtp_factory=lambda host, port: smtp,
    )


def _email_alert(severity: str = "warning") -> notify_mod.Alert:
    return notify_mod.Alert(
        kind="cost_regression",
        severity=severity,
        org_slug="acme",
        summary="run cost spiked 3x",
        details={"run_id": "abc", "cost_usd": 12.5},
    )


def test_email_notifier_rejects_empty_host():
    with pytest.raises(ValueError, match="smtp_host"):
        notify_mod.EmailNotifier(
            smtp_host="", smtp_port=25, username="", password="",
            from_addr="x@y", to_addrs=["a@b"],
        )


def test_email_notifier_rejects_empty_to_addrs():
    with pytest.raises(ValueError, match="to_addr"):
        notify_mod.EmailNotifier(
            smtp_host="smtp", smtp_port=25, username="", password="",
            from_addr="x@y", to_addrs=[],
        )


def test_email_notifier_rejects_empty_from():
    with pytest.raises(ValueError, match="from_addr"):
        notify_mod.EmailNotifier(
            smtp_host="smtp", smtp_port=25, username="", password="",
            from_addr="", to_addrs=["a@b"],
        )


def test_email_notifier_happy_path_sends_message():
    smtp = _FakeSMTP()
    n = _make_email_notifier(smtp)
    n.notify(_email_alert("warning"))

    assert smtp.starttls_called
    assert smtp.login_args == ("alerts@example.com", "hunter2")
    assert smtp.sendmail_args is not None
    from_addr, to_addrs, msg = smtp.sendmail_args
    assert from_addr == "alerts@example.com"
    assert to_addrs == ["oncall@example.com"]
    # Subject includes severity + org + summary.
    assert "Subject: [WARNING] acme: run cost spiked 3x" in msg
    # Body lists each detail key.
    assert "run_id: abc" in msg
    assert "cost_usd: 12.5" in msg
    assert smtp.quit_called


def test_email_notifier_skips_login_when_no_username():
    smtp = _FakeSMTP()
    n = _make_email_notifier(smtp, username="")
    n.notify(_email_alert())
    # login was not called (no creds) but mail still sent.
    assert smtp.login_args is None
    assert smtp.sendmail_args is not None


def test_email_notifier_drops_alert_if_starttls_unsupported(caplog):
    smtp = _FakeSMTP(starttls_raises=True)
    n = _make_email_notifier(smtp)
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        n.notify(_email_alert())
    # Did not attempt to login or send over plaintext.
    assert smtp.login_args is None
    assert smtp.sendmail_args is None
    assert any("STARTTLS" in r.message for r in caplog.records)


def test_email_notifier_swallows_sendmail_failure(caplog):
    smtp = _FakeSMTP(sendmail_raises=True)
    n = _make_email_notifier(smtp)
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        n.notify(_email_alert())  # must not raise
    assert any("sendmail failed" in r.message for r in caplog.records)


def test_default_notifier_returns_email_when_configured(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_PORT", "587")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv(
        "CLAUDESTRUCT_EMAIL_TO", "oncall@example.com,sre@example.com",
    )
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_USERNAME", "alerts@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_PASSWORD", "hunter2")

    n = notify_mod.default_notifier()
    assert n.name == "email"
    assert n._to == ["oncall@example.com", "sre@example.com"]


def test_default_notifier_email_missing_required_env_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.delenv("CLAUDESTRUCT_EMAIL_SMTP_HOST", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDESTRUCT_EMAIL_SMTP_HOST"):
        notify_mod.default_notifier()


def test_default_notifier_email_invalid_port_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_SMTP_PORT", "not-a-number")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_EMAIL_TO", "oncall@example.com")
    with pytest.raises(RuntimeError, match="not an integer"):
        notify_mod.default_notifier()


# --- AlertsScheduler (long-running daemon) --------------------------


def _seed_regression(env, *, now=None):
    """Seed the acme org with a baseline + one obviously-regressing run
    so a `compute_cost_regression_alerts` pass produces exactly one
    finding."""
    from datetime import datetime as _dt
    if now is None:
        now = _dt(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    factory = env["factory"]
    with factory() as session:
        # 5 cheap baseline runs — gives a clear mean ≈ 1.0 with stddev ≈ 0.
        for i in range(5):
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"baseline-{i}", cost=1.0,
                created_at=now - timedelta(days=2),
            )
        # One recent run that costs 100x baseline → blows past 2σ.
        _seed_run(
            session,
            org_id=env["acme_id"], user_id=env["ua_id"],
            run_id="spike", cost=100.0,
            created_at=now - timedelta(hours=1),
        )
        session.commit()
    return now


def test_scheduler_rejects_non_positive_interval(env):
    with pytest.raises(ValueError, match="interval_s"):
        alerts_mod.AlertsScheduler(
            session_factory=env["factory"],
            notifier=_CapturingNotifier(),
            interval_s=0,
        )


def test_scheduler_run_once_dispatches_findings(env, monkeypatch):
    """run_once executes a full pass and counts in passes_completed."""
    # Pin "now" so the seeded recent run lands inside check_recent_hours.
    fake_now = _seed_regression(env)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)

    notifier = _CapturingNotifier()
    sched = alerts_mod.AlertsScheduler(
        session_factory=env["factory"],
        notifier=notifier,
        interval_s=60.0,
    )
    n = sched.run_once()
    assert n == 1
    assert sched.passes_completed == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0].kind == "cost_regression"


def test_scheduler_run_once_swallows_pass_exceptions():
    """A crash inside the pass increments passes_completed but doesn't
    propagate — the daemon must keep running across transient failures."""
    class _ExplodingFactory:
        def __call__(self):
            raise RuntimeError("simulated DB blip")

    sched = alerts_mod.AlertsScheduler(
        session_factory=_ExplodingFactory(),
        notifier=_CapturingNotifier(),
        interval_s=60.0,
    )
    # No raise.
    n = sched.run_once()
    assert n == 0
    assert sched.passes_completed == 1


def test_scheduler_thread_lifecycle(env, monkeypatch):
    """start() spawns a daemon thread that runs at least one pass;
    stop() joins it cleanly within the timeout."""
    import time

    fake_now = _seed_regression(env)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)

    notifier = _CapturingNotifier()
    sched = alerts_mod.AlertsScheduler(
        session_factory=env["factory"],
        notifier=notifier,
        # Very short interval so the test doesn't drag.
        interval_s=0.05,
    )
    sched.start()
    # Wait until at least one pass has happened — the loop calls
    # run_once before the first sleep so this should be near-instant.
    deadline = time.monotonic() + 5.0
    while sched.passes_completed < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sched.passes_completed >= 1
    assert len(notifier.calls) >= 1

    sched.stop(timeout=2.0)
    # Thread must be done.
    assert sched._thread is not None
    assert not sched._thread.is_alive()


def test_scheduler_double_start_is_idempotent(env, monkeypatch):
    """Calling start() twice doesn't spawn a second thread."""
    monkeypatch.setattr(
        alerts_mod, "_now_utc",
        lambda: datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
    )
    sched = alerts_mod.AlertsScheduler(
        session_factory=env["factory"],
        notifier=_CapturingNotifier(),
        interval_s=10.0,
    )
    sched.start()
    first_thread = sched._thread
    sched.start()  # No-op on already-running scheduler.
    assert sched._thread is first_thread
    sched.stop(timeout=2.0)


def test_run_alert_pass_helper_returns_dispatched_count(env, monkeypatch):
    """The shared helper that both --once and --watch use."""
    fake_now = _seed_regression(env)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)

    notifier = _CapturingNotifier()
    n = alerts_mod.run_alert_pass(env["factory"], notifier)
    assert n == 1
    assert len(notifier.calls) == 1


# --- Team budget-cap rollup alerts (W6.5) ---------------------------


def _seed_team_with_spend(
    env, *, team_slug: str, member_emails: list[str], total_spend: float,
) -> int:
    """Create a team in the acme org, add the listed members (creating
    User rows as needed), and seed runs whose total cost = total_spend.

    Returns the team_id."""
    from sqlalchemy import select as _select

    from claudestruct.server.models import (
        Membership,
        Role,
        Team,
        TeamMembership,
    )
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        team = Team(
            org_id=env["acme_id"], slug=team_slug, name=team_slug.upper(),
        )
        session.add(team)
        session.flush()

        # Create users (and org-membership rows) for each email.
        member_ids: list[int] = []
        for email in member_emails:
            existing = session.execute(
                _select(User).where(User.email == email)
            ).scalar_one_or_none()
            if existing is None:
                u = User(email=email)
                session.add(u)
                session.flush()
                session.add(Membership(
                    user_id=u.id, org_id=env["acme_id"],
                    role=Role.member.value,
                ))
                member_ids.append(u.id)
            else:
                member_ids.append(existing.id)
            session.add(TeamMembership(team_id=team.id, user_id=member_ids[-1]))

        # Distribute spend across members so the rollup actually has
        # to sum across user_ids (catches a "first user only" bug).
        if member_ids and total_spend > 0:
            per_user = total_spend / len(member_ids)
            for i, uid in enumerate(member_ids):
                _seed_run(
                    session,
                    org_id=env["acme_id"], user_id=uid,
                    run_id=f"{team_slug}-r-{i}", cost=per_user,
                    created_at=now - timedelta(hours=2),
                )
        team_id = team.id
        session.commit()
    return team_id


def test_team_budget_no_findings_for_uncapped_tier(env, monkeypatch):
    """team / business tiers have no USD cap → nothing to compare
    against → no findings even if team has burned a lot."""
    from claudestruct.server import billing as billing_mod

    # Promote acme to team tier (uncapped).
    factory = env["factory"]
    with factory() as session:
        sub = billing_mod.get_or_default(session, env["acme_id"])
        sub.tier = billing_mod.Tier.team.value
        session.commit()

    _seed_team_with_spend(
        env, team_slug="platform",
        member_emails=["alice@acme"], total_spend=1000.0,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)

    with factory() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert findings == []


def test_team_budget_no_findings_below_threshold(env, monkeypatch):
    """Free tier cap is $10; team with $5 spend (50 %) is below the
    80 % warn threshold → no finding."""
    _seed_team_with_spend(
        env, team_slug="platform",
        member_emails=["alice@acme"], total_spend=5.0,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert findings == []


def test_team_budget_warning_when_above_80_percent(env, monkeypatch):
    """Free cap = $10; team has spent $8.50 (85 %) → warning."""
    _seed_team_with_spend(
        env, team_slug="platform",
        member_emails=["alice@acme"], total_spend=8.50,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert len(findings) == 1
    f = findings[0]
    assert f.team_slug == "platform"
    assert f.team_cost_usd == pytest.approx(8.50)
    assert f.org_cap_usd == pytest.approx(10.0)
    assert f.ratio == pytest.approx(0.85)
    alert = alerts_mod.team_budget_finding_to_alert(f)
    assert alert.kind == "team_budget_burn"
    assert alert.severity == "warning"
    assert "85.0%" in alert.summary or "85%" in alert.summary


def test_team_budget_critical_when_at_or_above_cap(env, monkeypatch):
    """Free cap = $10; team has spent $12 (120 %) → critical."""
    _seed_team_with_spend(
        env, team_slug="growth",
        member_emails=["bob@acme"], total_spend=12.0,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert len(findings) == 1
    alert = alerts_mod.team_budget_finding_to_alert(findings[0])
    assert alert.severity == "critical"


def test_team_budget_sums_across_team_members(env, monkeypatch):
    """The rollup must sum spend across every user_id on the team —
    a 'first user only' bug would slip through with a single-member
    team. Two members at $4.50 each = $9 (90 %) → warning."""
    _seed_team_with_spend(
        env, team_slug="platform",
        member_emails=["alice@acme", "bob@acme"], total_spend=9.0,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert len(findings) == 1
    assert findings[0].team_cost_usd == pytest.approx(9.0)
    assert findings[0].ratio == pytest.approx(0.9)


def test_team_budget_skips_empty_teams(env, monkeypatch):
    """Team with zero TeamMembership rows → skipped, no division
    against the cap (the spend is 0 by definition)."""
    _seed_team_with_spend(
        env, team_slug="empty",
        member_emails=[], total_spend=0.0,
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert findings == []


def test_team_budget_findings_sort_by_ratio_desc(env, monkeypatch):
    """Operator scanning a Slack channel sees the most-burned team
    first."""
    _seed_team_with_spend(
        env, team_slug="moderate",
        member_emails=["a@acme"], total_spend=8.5,  # 85 %
    )
    _seed_team_with_spend(
        env, team_slug="severe",
        member_emails=["b@acme"], total_spend=12.0,  # 120 %
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    with env["factory"]() as session:
        findings = alerts_mod.compute_team_budget_alerts(session)
    assert len(findings) == 2
    assert findings[0].team_slug == "severe"
    assert findings[1].team_slug == "moderate"


def test_team_budget_findings_dispatched_through_notifier(env, monkeypatch):
    """End-to-end: run_alert_pass dispatches BOTH cost-regression and
    team-budget findings through a single notifier (so the same daemon
    handles both kinds)."""
    _seed_team_with_spend(
        env, team_slug="platform",
        member_emails=["alice@acme"], total_spend=9.0,  # 90 %
    )
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)

    notifier = _CapturingNotifier()
    n = alerts_mod.run_alert_pass(env["factory"], notifier)
    assert n == 1  # one team finding (the seeded data has no regression)
    assert notifier.calls[0].kind == "team_budget_burn"


# --- SLO error-budget burn-rate alerts (A.2) ------------------------


def _seed_burn_runs(
    env,
    *,
    now: datetime,
    total: int,
    failed: int,
    age_minutes: int,
) -> None:
    """Seed `total` runs landing `age_minutes` ago, of which `failed`
    are in `failed` status. Used to drive the burn-rate detector."""
    factory = env["factory"]
    with factory() as session:
        for i in range(total):
            status = (
                RunStatus.failed.value if i < failed
                else RunStatus.done.value
            )
            _seed_run(
                session,
                org_id=env["acme_id"], user_id=env["ua_id"],
                run_id=f"burn-{age_minutes}m-{i}", cost=0.1,
                status=status,
                created_at=now - timedelta(minutes=age_minutes),
            )
        session.commit()


def test_burn_rate_no_findings_when_low_traffic(env, monkeypatch):
    """Below `BURN_RATE_MIN_RUNS` the detector skips — at low traffic
    a single failure dominates the rate and we'd just yell 100% burn
    every cron tick."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    # 5 runs, 2 failed (40% error) — should NOT alert because total < 10.
    _seed_burn_runs(env, now=fake_now, total=5, failed=2, age_minutes=10)
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    assert findings == []


def test_burn_rate_no_findings_below_threshold(env, monkeypatch):
    """20 runs in 1h with 0 failures → 0× burn → no finding."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    _seed_burn_runs(env, now=fake_now, total=20, failed=0, age_minutes=10)
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    assert findings == []


def test_burn_rate_fast_burn_in_1h_window_critical(env, monkeypatch):
    """100 runs in 1h, 5 failed (5% error rate) → 50× burn rate
    → fires fast-burn (1h) finding with critical severity."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    _seed_burn_runs(env, now=fake_now, total=100, failed=5, age_minutes=15)
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    # Both 1h and 6h windows fire (the data lives in both); the 1h
    # finding must carry critical severity.
    by_window = {f.window: f for f in findings}
    assert "1h" in by_window
    fast = by_window["1h"]
    assert fast.failed == 5
    assert fast.total_runs == 100
    assert fast.error_rate == pytest.approx(0.05)
    # 0.05 / 0.001 = 50× burn rate.
    assert fast.burn_rate == pytest.approx(50.0)
    alert = alerts_mod.burn_rate_finding_to_alert(fast)
    assert alert.kind == "slo_error_budget_burn"
    assert alert.severity == "critical"


def test_burn_rate_slow_burn_in_6h_window_warning(env, monkeypatch):
    """500 runs in the 6h window with 5 failed (1% error rate)
    → 10× burn → fires the 6h slow-burn finding (warning) but
    NOT the 1h fast-burn (only ~83 runs land in the 1h window)."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    # Seed runs spread across 6h; only 1 failure in the most-recent
    # 1h slice → 1h burn rate too low to fire fast-burn.
    _seed_burn_runs(
        env, now=fake_now, total=400, failed=4, age_minutes=120,
    )
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    by_window = {f.window: f for f in findings}
    # 1h window — runs landed 120 min ago, so the 1h window is empty;
    # no fast-burn finding for this org.
    assert "1h" not in by_window
    assert "6h" in by_window
    slow = by_window["6h"]
    assert slow.total_runs == 400
    assert slow.failed == 4
    assert slow.error_rate == pytest.approx(0.01)
    # 0.01 / 0.001 = 10× burn rate; ≥ SLOW_BURN_THRESHOLD (6×).
    assert slow.burn_rate == pytest.approx(10.0)
    alert = alerts_mod.burn_rate_finding_to_alert(slow)
    assert alert.severity == "warning"


def test_burn_rate_findings_sorted_highest_first(env, monkeypatch):
    """When both 1h and 6h fire, they're sorted by burn_rate desc."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    _seed_burn_runs(env, now=fake_now, total=100, failed=10, age_minutes=15)
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    assert len(findings) == 2
    assert findings[0].burn_rate >= findings[1].burn_rate


def test_burn_rate_summary_includes_window_and_rate(env, monkeypatch):
    """The Slack/email body should let an operator triage without
    drilling into the JSON details."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    _seed_burn_runs(env, now=fake_now, total=50, failed=5, age_minutes=15)
    with env["factory"]() as session:
        findings = alerts_mod.compute_burn_rate_alerts(session, now=fake_now)
    alert = alerts_mod.burn_rate_finding_to_alert(findings[0])
    assert "acme" in alert.summary
    assert "1h" in alert.summary
    assert "100.0×" in alert.summary or "100.0x" in alert.summary or "100×" in alert.summary
    # Severity and rate must round-trip in the structured details too.
    assert alert.details["window"] == "1h"
    assert alert.details["burn_rate"] == 100.0


def test_burn_rate_findings_dispatched_through_notifier(env, monkeypatch):
    """End-to-end: run_alert_pass dispatches all three detector kinds
    (cost-regression, team-budget, burn-rate) through a single
    notifier so the daemon handles them as one stream."""
    fake_now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alerts_mod, "_now_utc", lambda: fake_now)
    _seed_burn_runs(env, now=fake_now, total=50, failed=10, age_minutes=15)

    notifier = _CapturingNotifier()
    n = alerts_mod.run_alert_pass(env["factory"], notifier)
    assert n >= 1
    kinds = {c.kind for c in notifier.calls}
    assert "slo_error_budget_burn" in kinds
