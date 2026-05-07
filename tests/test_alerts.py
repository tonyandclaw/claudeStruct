"""Tests for the notification surface + cost-regression detector (W6.5)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

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
    """Captures SMTP traffic for EmailNotifier tests.

    Mirrors enough of the ``smtplib.SMTP`` surface that ``EmailNotifier``
    can drive it: ``starttls`` / ``login`` / ``send_message`` / ``quit``.
    """

    def __init__(self) -> None:
        self.host: str | None = None
        self.port: int | None = None
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: list[Any] = []
        self.quit_called = False
        self.starttls_exc: Exception | None = None
        self.login_exc: Exception | None = None
        self.send_exc: Exception | None = None

    def starttls(self) -> None:
        self.starttls_called = True
        if self.starttls_exc is not None:
            raise self.starttls_exc

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)
        if self.login_exc is not None:
            raise self.login_exc

    def send_message(self, msg: Any) -> None:
        if self.send_exc is not None:
            raise self.send_exc
        self.sent.append(msg)

    def quit(self) -> None:
        self.quit_called = True


def _email_notifier(smtp: _FakeSMTP, **overrides) -> notify_mod.EmailNotifier:
    """Common EmailNotifier ctor wrapper for tests."""
    def factory(host: str, port: int) -> _FakeSMTP:
        smtp.host = host
        smtp.port = port
        return smtp

    kwargs: dict[str, Any] = dict(
        host="smtp.example.com",
        port=587,
        sender="alerts@example.com",
        recipients=["oncall@example.com"],
        smtp_factory=factory,
    )
    kwargs.update(overrides)
    return notify_mod.EmailNotifier(**kwargs)


def test_email_notifier_rejects_empty_host():
    with pytest.raises(ValueError, match="SMTP host"):
        notify_mod.EmailNotifier(
            host="",
            sender="a@b.test",
            recipients=["c@d.test"],
        )


def test_email_notifier_rejects_empty_sender():
    with pytest.raises(ValueError, match="sender"):
        notify_mod.EmailNotifier(
            host="smtp.example.com",
            sender="",
            recipients=["c@d.test"],
        )


def test_email_notifier_rejects_no_recipients():
    with pytest.raises(ValueError, match="recipient"):
        notify_mod.EmailNotifier(
            host="smtp.example.com",
            sender="a@b.test",
            recipients=[],
        )


def test_email_notifier_sends_message_with_starttls_and_login():
    smtp = _FakeSMTP()
    notifier = _email_notifier(
        smtp,
        username="alerts@example.com",
        password="hunter2",
        recipients=["oncall@example.com", "sre@example.com"],
    )
    alert = notify_mod.Alert(
        kind="cost_regression",
        severity="critical",
        org_slug="acme",
        summary="run r1 cost $10 is 5σ above mean",
        details={"run_id": "r1", "sigma_above": 5.0},
    )
    notifier.notify(alert)

    assert smtp.starttls_called is True
    assert smtp.login_args == ("alerts@example.com", "hunter2")
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert "[CRITICAL]" in msg["Subject"]
    assert "acme" in msg["Subject"]
    # Multiple recipients render as a comma-joined To header.
    assert "oncall@example.com" in msg["To"]
    assert "sre@example.com" in msg["To"]
    body = msg.get_content()
    assert "Severity: critical" in body
    assert "run_id: r1" in body
    assert smtp.quit_called is True


def test_email_notifier_skips_login_when_no_credentials():
    smtp = _FakeSMTP()
    notifier = _email_notifier(smtp, use_starttls=False)
    notifier.notify(notify_mod.Alert(
        kind="x", severity="info", org_slug="acme",
        summary="s", details={},
    ))
    assert smtp.starttls_called is False
    assert smtp.login_args is None
    assert len(smtp.sent) == 1


def test_email_notifier_swallows_send_failure(caplog):
    smtp = _FakeSMTP()
    smtp.send_exc = RuntimeError("connection reset")
    notifier = _email_notifier(smtp)
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(notify_mod.Alert(
            kind="x", severity="info", org_slug="acme",
            summary="s", details={},
        ))
    assert any("send failed" in r.message for r in caplog.records)
    assert smtp.quit_called is True  # quit always runs in finally


def test_email_notifier_swallows_login_failure_without_sending(caplog):
    smtp = _FakeSMTP()
    smtp.login_exc = RuntimeError("auth rejected")
    notifier = _email_notifier(
        smtp, username="u", password="p",
    )
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(notify_mod.Alert(
            kind="x", severity="info", org_slug="acme",
            summary="s", details={},
        ))
    assert any("login failed" in r.message for r in caplog.records)
    assert smtp.sent == []  # login failure short-circuits before send


def test_email_notifier_swallows_starttls_failure_and_continues_to_send(caplog):
    """STARTTLS refusal logs but still attempts the send — matches
    Slack's swallow-and-log policy so cron jobs don't die on a TLS
    misconfig that the operator can fix later."""
    smtp = _FakeSMTP()
    smtp.starttls_exc = RuntimeError("server refused starttls")
    notifier = _email_notifier(smtp)
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(notify_mod.Alert(
            kind="x", severity="info", org_slug="acme",
            summary="s", details={},
        ))
    assert any("starttls failed" in r.message for r in caplog.records)
    assert len(smtp.sent) == 1


def test_email_notifier_swallows_connect_failure(caplog):
    """SMTP connect failure must not bubble up to the caller — matches
    SlackWebhookNotifier behaviour so the cron driver stays alive."""
    def factory(host: str, port: int) -> _FakeSMTP:
        raise OSError("connection refused")

    notifier = notify_mod.EmailNotifier(
        host="smtp.example.com",
        sender="a@b.test",
        recipients=["c@d.test"],
        smtp_factory=factory,
    )
    with caplog.at_level("WARNING", logger="claudestruct.notify"):
        notifier.notify(notify_mod.Alert(
            kind="x", severity="info", org_slug="acme",
            summary="s", details={},
        ))
    assert any("smtp connect failed" in r.message for r in caplog.records)


def test_split_recipients_handles_mixed_separators():
    out = notify_mod._split_recipients("a@x.test, b@x.test;c@x.test , ")
    assert out == ["a@x.test", "b@x.test", "c@x.test"]


def test_default_notifier_returns_email_when_configured(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_TO", "oncall@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_PORT", "2525")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_STARTTLS", "0")
    n = notify_mod.default_notifier()
    assert n.name == "email"
    assert n._port == 2525
    assert n._use_starttls is False
    assert n._recipients == ["oncall@example.com"]


def test_default_notifier_email_missing_host_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.delenv("CLAUDESTRUCT_SMTP_HOST", raising=False)
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_TO", "oncall@example.com")
    with pytest.raises(RuntimeError, match="CLAUDESTRUCT_SMTP_HOST"):
        notify_mod.default_notifier()


def test_default_notifier_email_missing_recipients_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_FROM", "alerts@example.com")
    monkeypatch.delenv("CLAUDESTRUCT_SMTP_TO", raising=False)
    with pytest.raises(RuntimeError, match="CLAUDESTRUCT_SMTP_TO"):
        notify_mod.default_notifier()


def test_default_notifier_email_invalid_port_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "email")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_TO", "oncall@example.com")
    monkeypatch.setenv("CLAUDESTRUCT_SMTP_PORT", "not-a-number")
    with pytest.raises(RuntimeError, match="not a valid integer"):
        notify_mod.default_notifier()


# --- cs serve alerts --watch (scheduler mode) ----------------------


def test_serve_alerts_watch_runs_bounded_passes_then_exits(tmp_path, monkeypatch):
    """`cs serve alerts --watch --max-iterations 2` must call the
    detector twice + sleep once between, then exit 0. Uses a no-op
    sleep monkeypatch so the test stays fast."""
    from click.testing import CliRunner

    from claudestruct.server.cli import serve_group

    # Sleep stub: record durations + return immediately. Lets us pin
    # the loop's interval semantics without an actual wall-clock wait.
    sleeps: list[float] = []
    monkeypatch.setattr(
        "time.sleep",
        lambda s: sleeps.append(s),
    )

    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "log")

    db_url = "sqlite:///" + str(tmp_path / "watch.db")
    result = CliRunner().invoke(
        serve_group,
        [
            "alerts",
            "--db-url", db_url,
            "--watch",
            "--interval", "5",
            "--max-iterations", "2",
        ],
    )
    assert result.exit_code == 0, result.output
    # First pass + sleep + second pass — N-1 sleeps for N iterations.
    assert sleeps == [5]
    # Both passes log a dispatched-count line.
    assert "alerts[1]:" in result.output
    assert "alerts[2]:" in result.output


def test_serve_alerts_watch_invalid_interval_rejected(tmp_path):
    """`--interval 0` (or negative) is a config error — exit non-zero
    with a clear message rather than spinning at full CPU."""
    from click.testing import CliRunner

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "bad.db")
    result = CliRunner().invoke(
        serve_group,
        [
            "alerts",
            "--db-url", db_url,
            "--watch",
            "--interval", "0",
            "--max-iterations", "1",
        ],
    )
    assert result.exit_code != 0
    assert "--interval must be > 0" in result.output


def test_serve_alerts_watch_continues_after_pass_error(tmp_path, monkeypatch):
    """A transient failure in one pass (DB blip, etc.) must NOT tear
    the watcher down — log the error and keep polling. Verified by
    forcing the first call to ``compute_cost_regression_alerts`` to
    raise, then letting the second call succeed."""
    from click.testing import CliRunner

    from claudestruct.server import cli as cli_mod
    from claudestruct.server.alerts import compute_cost_regression_alerts

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("CLAUDESTRUCT_NOTIFY_PROVIDER", "log")

    real_calls: list[int] = []

    def flaky(session, **kwargs):
        real_calls.append(1)
        if len(real_calls) == 1:
            raise RuntimeError("transient db blip")
        return compute_cost_regression_alerts(session, **kwargs)

    monkeypatch.setattr(
        "claudestruct.server.alerts.compute_cost_regression_alerts",
        flaky,
    )

    db_url = "sqlite:///" + str(tmp_path / "flaky.db")
    result = CliRunner().invoke(
        cli_mod.serve_group,
        [
            "alerts",
            "--db-url", db_url,
            "--watch",
            "--interval", "1",
            "--max-iterations", "2",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert len(real_calls) == 2
    # The first pass's error surfaces but the loop survives.
    combined = result.output + (result.stderr if result.stderr else "")
    assert "transient db blip" in combined or "error" in combined.lower()
