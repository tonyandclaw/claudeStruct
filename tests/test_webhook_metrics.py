"""Tests for the webhook-error counter (C.3)."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi")

from claudestruct.server.webhook_metrics import WEBHOOK_ERRORS, WebhookErrorCounter


@pytest.fixture(autouse=True)
def _reset_global():
    """The module-level singleton is shared with other test files;
    reset before AND after so this suite never leaves a drift behind."""
    WEBHOOK_ERRORS.reset()
    yield
    WEBHOOK_ERRORS.reset()


def test_bump_increments_path_reason_pair():
    c = WebhookErrorCounter()
    c.bump(path="github.verdict_comment", reason="github_app_error")
    c.bump(path="github.verdict_comment", reason="github_app_error")
    assert c.get(path="github.verdict_comment", reason="github_app_error") == 2


def test_bump_path_reason_keys_are_independent():
    """Each ``(path, reason)`` pair gets its own counter so the
    Prometheus output is machine-greppable."""
    c = WebhookErrorCounter()
    c.bump(path="github.pr_open", reason="github_app_error")
    c.bump(path="github.pr_open", reason="unexpected")
    c.bump(path="stripe.subscription_retrieve", reason="stripe_error")
    assert c.get(path="github.pr_open", reason="github_app_error") == 1
    assert c.get(path="github.pr_open", reason="unexpected") == 1
    assert c.get(path="stripe.subscription_retrieve", reason="stripe_error") == 1
    assert c.total() == 3


def test_bump_with_zero_or_negative_is_noop():
    """A defensive guard so a `bump(by=0)` from a future caller can't
    decrement the counter (the data type is a counter, not a gauge)."""
    c = WebhookErrorCounter()
    c.bump(path="x", reason="y", by=0)
    c.bump(path="x", reason="y", by=-5)
    assert c.get(path="x", reason="y") == 0


def test_reset_zeros_every_pair():
    c = WebhookErrorCounter()
    c.bump(path="a", reason="b")
    c.bump(path="c", reason="d")
    c.reset()
    assert c.total() == 0


def test_render_prometheus_produces_expected_shape():
    c = WebhookErrorCounter()
    c.bump(path="github.pr_open", reason="github_app_error", by=3)
    out = c.render_prometheus()
    assert "# HELP claudestruct_webhook_errors_total" in out
    assert "# TYPE claudestruct_webhook_errors_total counter" in out
    assert (
        'claudestruct_webhook_errors_total'
        '{path="github.pr_open",reason="github_app_error"} 3'
    ) in out


def test_render_prometheus_escapes_label_quotes():
    """Prometheus label values must escape backslashes and quotes —
    a malformed label value silently breaks the scrape parser."""
    c = WebhookErrorCounter()
    c.bump(path='weird"path', reason='weird\\reason')
    out = c.render_prometheus()
    assert 'weird\\"path' in out
    assert 'weird\\\\reason' in out


def test_concurrent_bumps_do_not_lose_counts():
    """Each `bump` is wrapped in a lock; under concurrent load the
    final count must equal the number of calls (no torn writes)."""
    c = WebhookErrorCounter()
    ITERATIONS = 200
    THREADS = 8

    def hammer():
        for _ in range(ITERATIONS):
            c.bump(path="contended", reason="r")

    threads = [threading.Thread(target=hammer) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.get(path="contended", reason="r") == ITERATIONS * THREADS


# --- Integration via FastAPI route ----------------------------------


def test_webhook_errors_endpoint_renders_singleton(tmp_path):
    """The /v1/slo/webhook_errors endpoint reads the module-level
    singleton, so a bump from anywhere in the codebase should
    appear in the response."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from claudestruct.server.app import create_app
    from claudestruct.server.db import init_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    client = TestClient(app)

    WEBHOOK_ERRORS.bump(
        path="github.verdict_comment", reason="github_app_error", by=2,
    )
    r = client.get("/v1/slo/webhook_errors")
    assert r.status_code == 200
    body = r.text
    assert (
        'claudestruct_webhook_errors_total'
        '{path="github.verdict_comment",reason="github_app_error"} 2'
    ) in body


def test_webhook_errors_endpoint_unauthenticated(tmp_path):
    """The metrics endpoint must be reachable without a bearer token
    so a Prometheus scraper on the private network can read it."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from claudestruct.server.app import create_app
    from claudestruct.server.db import init_db

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    client = TestClient(app)

    r = client.get("/v1/slo/webhook_errors")
    # No Authorization header at all → still 200, not 401.
    assert r.status_code == 200


# --- Worker swallow paths bump the counter --------------------------


def _fake_run_with_github_context():
    """Build an in-memory Run with the github_* fields populated so
    the swallow helpers actually attempt their outbound IO."""
    from claudestruct.server.models import Run, RunStatus
    return Run(
        run_id="test-run",
        org_id=1,
        user_id=1,
        status=RunStatus.done.value,
        task="dev",
        description="x",
        cost_usd=0.5,
        github_installation_id=12345,
        github_repo_full_name="acme/repo",
        github_pr_number=42,
        github_check_run_id=99,
        github_base_branch="main",
    )


def test_worker_verdict_comment_failure_bumps_counter(monkeypatch):
    """When `post_ack_comment` raises GitHubAppError, the swallow
    path must bump `path=github.verdict_comment, reason=github_app_error`
    so the silent failure is visible in /v1/slo/webhook_errors."""
    from claudestruct.server import github_app as gh_mod
    from claudestruct.server import worker as worker_mod
    from claudestruct.server.github_app import GitHubAppError

    # Stub out the deps so the helper actually reaches the swallow path.
    monkeypatch.setattr(
        worker_mod, "_get_verdict_token_cache", lambda: object(),
    )
    # Provide a fake config so the early-return guard doesn't fire.

    class _FakeCfg:
        pass

    monkeypatch.setattr(gh_mod, "load_app_config", lambda: _FakeCfg())
    monkeypatch.setattr(
        gh_mod, "post_ack_comment",
        lambda **kw: (_ for _ in ()).throw(GitHubAppError("simulated 500")),
    )

    # The worker reads http_client_factory at module scope; an HTTP
    # client we control is enough to dodge the real `httpx`.

    class _FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(
        worker_mod, "_http_client_default", lambda: _FakeClient(),
    )

    run = _fake_run_with_github_context()
    worker_mod._post_verdict_comment_safe(run)  # must not raise

    assert WEBHOOK_ERRORS.get(
        path="github.verdict_comment", reason="github_app_error",
    ) == 1
    assert WEBHOOK_ERRORS.get(
        path="github.verdict_comment", reason="unexpected",
    ) == 0


def test_worker_verdict_comment_unexpected_exception_bumps_unexpected(monkeypatch):
    """Non-GitHubAppError exceptions land on the `reason=unexpected`
    branch so operators can distinguish 'GitHub returned a 5xx' from
    'our code crashed'."""
    from claudestruct.server import github_app as gh_mod
    from claudestruct.server import worker as worker_mod

    monkeypatch.setattr(
        worker_mod, "_get_verdict_token_cache", lambda: object(),
    )

    class _FakeCfg:
        pass

    monkeypatch.setattr(gh_mod, "load_app_config", lambda: _FakeCfg())
    monkeypatch.setattr(
        gh_mod, "post_ack_comment",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("totally unexpected")),
    )

    class _FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(
        worker_mod, "_http_client_default", lambda: _FakeClient(),
    )

    run = _fake_run_with_github_context()
    worker_mod._post_verdict_comment_safe(run)

    assert WEBHOOK_ERRORS.get(
        path="github.verdict_comment", reason="unexpected",
    ) == 1
    assert WEBHOOK_ERRORS.get(
        path="github.verdict_comment", reason="github_app_error",
    ) == 0
