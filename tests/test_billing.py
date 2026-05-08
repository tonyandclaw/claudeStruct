"""Tests for the billing skeleton (W8.2).

Covers:
- Subscription model lifecycle (`get_or_default` materializes a free
  placeholder; subsequent calls return the same row)
- Tier-driven audit retention map
- ``current_period_bounds`` falls back to the UTC calendar month
- HTTP routes: ``GET /v1/billing/subscription``, ``POST /v1/billing/checkout``,
  ``GET /v1/billing/usage``, ``POST /v1/billing/webhook``
- Stripe SDK gating (webhook returns 503 when the SDK isn't installed)

The Stripe SDK is not installed in this test env -- ``stripe_sdk_available()``
returns False and the webhook path falls through to the 503 branch.
That's exactly the OSS self-host shape we want to ship.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server import billing as billing_mod
from claudestruct.server.app import create_app
from claudestruct.server.auth import generate_key
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import ApiKey, Membership, Org, Role, User


@pytest.fixture()
def env(tmp_path):
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
            ("admin@a", Role.admin),
            ("member@a", Role.member),
            ("viewer@a", Role.viewer),
        ]:
            user = User(email=email)
            session.add(user)
            session.flush()
            session.add(Membership(user_id=user.id, org_id=org.id, role=role.value))
            full, key_id, hashed = generate_key()
            session.add(ApiKey(
                user_id=user.id, org_id=org.id,
                key_id=key_id, hashed_secret=hashed, name=f"{email}-key",
            ))
            keys[email] = full
        session.commit()
    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    return {"client": TestClient(app), "keys": keys, "factory": factory,
            "tmp_path": tmp_path}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Model + helpers ------------------------------------------------

def test_get_or_default_materializes_free_tier(env):
    factory = env["factory"]
    with factory() as session:
        org_id = session.query(Org).first().id
        sub = billing_mod.get_or_default(session, org_id)
        session.commit()
        assert sub.tier == billing_mod.Tier.free.value
        # Second call returns the same row, not a duplicate.
        sub2 = billing_mod.get_or_default(session, org_id)
        assert sub2.id == sub.id


def test_audit_retention_days_per_tier():
    assert billing_mod.AUDIT_RETENTION_DAYS[billing_mod.Tier.free] == 90
    assert billing_mod.AUDIT_RETENTION_DAYS[billing_mod.Tier.team] == 365 * 7
    assert billing_mod.AUDIT_RETENTION_DAYS[billing_mod.Tier.business] == 365 * 7


def test_current_period_bounds_uses_stripe_window_when_set():
    sub = billing_mod.Subscription(
        org_id=1,
        tier=billing_mod.Tier.team.value,
        current_period_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        current_period_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    start, end = billing_mod.current_period_bounds(sub)
    assert start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_current_period_bounds_falls_back_to_calendar_month():
    sub = billing_mod.Subscription(
        org_id=1, tier=billing_mod.Tier.free.value,
    )
    now = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
    start, end = billing_mod.current_period_bounds(sub, now=now)
    assert start == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_calendar_month_handles_december_rollover():
    sub = billing_mod.Subscription(org_id=1, tier=billing_mod.Tier.free.value)
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    start, end = billing_mod.current_period_bounds(sub, now=now)
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_stub_checkout_url_is_deterministic():
    a = billing_mod.stub_checkout_url(org_slug="acme", tier=billing_mod.Tier.team)
    b = billing_mod.stub_checkout_url(org_slug="acme", tier=billing_mod.Tier.team)
    assert a == b
    c = billing_mod.stub_checkout_url(org_slug="acme", tier=billing_mod.Tier.business)
    assert c != a


# --- HTTP routes ----------------------------------------------------

def test_subscription_unauthenticated_401(env):
    r = env["client"].get("/v1/billing/subscription")
    assert r.status_code == 401


def test_subscription_returns_free_tier_for_new_org(env):
    r = env["client"].get(
        "/v1/billing/subscription", headers=_auth(env["keys"]["member@a"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "free"
    assert body["org_slug"] == "acme"
    assert body["status"] is None
    assert body["stripe_customer_id"] is None
    # W8.3: sandbox limits surface alongside the tier so the SPA /
    # CLI can render quota state without re-implementing the lookup.
    assert body["sandbox_limits"] == {
        "max_concurrent_runs": 1,
        "max_runtime_seconds": 300,
        "max_cost_usd": 0.5,
    }


def test_subscription_member_can_read_admin_can_too(env):
    for email in ("member@a", "admin@a"):
        r = env["client"].get(
            "/v1/billing/subscription", headers=_auth(env["keys"][email]),
        )
        assert r.status_code == 200, email


def test_checkout_admin_only(env):
    r = env["client"].post(
        "/v1/billing/checkout",
        json={"tier": "team", "success_url": "https://example.com/ok",
              "cancel_url": "https://example.com/cancel"},
        headers=_auth(env["keys"]["member@a"]),
    )
    assert r.status_code == 403


def test_checkout_returns_stub_url(env):
    r = env["client"].post(
        "/v1/billing/checkout",
        json={"tier": "team", "success_url": "https://example.com/ok",
              "cancel_url": "https://example.com/cancel"},
        headers=_auth(env["keys"]["admin@a"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["url"].startswith("https://checkout.example.invalid/")
    assert body["checkout_session_id"]


def test_checkout_audited(env):
    headers = _auth(env["keys"]["admin@a"])
    env["client"].post(
        "/v1/billing/checkout",
        json={"tier": "team", "success_url": "https://example.com/ok",
              "cancel_url": "https://example.com/cancel"},
        headers=headers,
    )
    listed = env["client"].get("/v1/audit", headers=headers).json()
    actions = [e["action"] for e in listed["entries"]]
    assert "billing.checkout.create" in actions


def test_usage_returns_zero_for_empty_run_log(env):
    r = env["client"].get(
        "/v1/billing/usage", headers=_auth(env["keys"]["member@a"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["input_tokens"] == 0
    assert body["output_tokens"] == 0
    assert body["cost_usd"] == 0


def test_usage_aggregates_runs_in_current_period(env):
    runs_dir = env["tmp_path"] / ".claudestruct" / "runs"
    runs_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    (runs_dir / "r1.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "run.start", "ts": now.isoformat(),
                        "task": "dev", "model": "claude-opus-4-7",
                        "effort": "high", "promptVersion": "v=abc"}),
            json.dumps({"type": "agent.usage", "inputTokens": 1234,
                        "outputTokens": 567, "cacheReadTokens": 100,
                        "cacheCreationTokens": 50, "costUsd": 1.50}),
            json.dumps({"type": "run.end", "ts": now.isoformat(),
                        "reason": "complete", "durationMs": 60000,
                        "totalCostUsd": 1.50}),
        ]),
        encoding="utf-8",
    )
    r = env["client"].get("/v1/billing/usage", headers=_auth(env["keys"]["member@a"]))
    assert r.status_code == 200
    body = r.json()
    assert body["input_tokens"] == 1234
    assert body["output_tokens"] == 567
    assert body["cost_usd"] == pytest.approx(1.50)


def test_webhook_503_when_stripe_not_installed(env):
    # In the test env, stripe SDK is not installed -- the route returns
    # 503 with a clear "install stripe" message.
    r = env["client"].post(
        "/v1/billing/webhook",
        headers={"Stripe-Signature": "t=0,v1=fake"},
        content=b"{}",
    )
    assert r.status_code == 503
    assert "stripe" in r.json()["detail"].lower()


# --- /v1/billing/invoices/{id}/pdf passthrough (W8.2) ---------------


class _FakeStripeError(Exception):
    pass


class _FakeInvalidRequestError(_FakeStripeError):
    pass


class _FakeInvoice:
    """Mimics the subset of stripe.Invoice that our route reads.

    Real `stripe.Invoice` is dict-like but also exposes attribute
    access. The route handles both; we use attribute access here so
    the test catches a regression that depends on `__getitem__`.
    """

    def __init__(
        self,
        *,
        customer: str,
        invoice_pdf: str | None = "https://files.stripe.com/x.pdf",
        hosted_invoice_url: str | None = "https://invoice.stripe.com/y",
    ) -> None:
        self.customer = customer
        self.invoice_pdf = invoice_pdf
        self.hosted_invoice_url = hosted_invoice_url


def _install_fake_stripe(
    monkeypatch,
    *,
    invoice: _FakeInvoice | None = None,
    raise_invalid: bool = False,
    raise_other: bool = False,
):
    """Inject a fake `stripe` module into sys.modules so the route's
    `import stripe` succeeds. Also flips `stripe_sdk_available()` to
    True for the duration of the test."""
    import sys
    import types

    fake = types.ModuleType("stripe")
    fake.StripeError = _FakeStripeError  # type: ignore[attr-defined]
    fake.InvalidRequestError = _FakeInvalidRequestError  # type: ignore[attr-defined]

    class _InvoiceNs:
        @staticmethod
        def retrieve(invoice_id: str):
            if raise_invalid:
                raise _FakeInvalidRequestError("no such invoice")
            if raise_other:
                raise _FakeStripeError("upstream timeout")
            return invoice

    fake.Invoice = _InvoiceNs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(billing_mod, "stripe_sdk_available", lambda: True)


def _seed_subscription_with_customer(env, customer_id: str | None) -> None:
    """Set the org's subscription.stripe_customer_id directly."""
    factory = env["factory"]
    with factory() as session:
        org_id = session.query(Org).first().id
        sub = billing_mod.get_or_default(session, org_id)
        sub.stripe_customer_id = customer_id
        session.commit()


def test_invoice_pdf_503_when_stripe_not_installed(env):
    """Without stripe SDK, the route fails closed with 503."""
    r = env["client"].get(
        "/v1/billing/invoices/in_test/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 503
    assert "stripe" in r.json()["detail"].lower()


def test_invoice_pdf_unauthenticated_401(env, monkeypatch):
    _install_fake_stripe(monkeypatch, invoice=_FakeInvoice(customer="cus_x"))
    r = env["client"].get("/v1/billing/invoices/in_test/pdf")
    assert r.status_code == 401


def test_invoice_pdf_404_when_org_has_no_stripe_customer(env, monkeypatch):
    """Org never went through Checkout → no stripe_customer_id →
    404 (must be indistinguishable from a wrong / unknown invoice)."""
    _install_fake_stripe(monkeypatch, invoice=_FakeInvoice(customer="cus_other"))
    # Org's stripe_customer_id is left None (default).
    r = env["client"].get(
        "/v1/billing/invoices/in_test/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "invoice not found"


def test_invoice_pdf_happy_path(env, monkeypatch):
    """Caller's stripe_customer_id matches invoice.customer →
    return the PDF URL."""
    _seed_subscription_with_customer(env, customer_id="cus_acme")
    _install_fake_stripe(
        monkeypatch,
        invoice=_FakeInvoice(
            customer="cus_acme",
            invoice_pdf="https://files.stripe.com/abc.pdf",
            hosted_invoice_url="https://invoice.stripe.com/i/abc",
        ),
    )
    r = env["client"].get(
        "/v1/billing/invoices/in_test/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["invoice_id"] == "in_test"
    assert body["invoice_pdf_url"] == "https://files.stripe.com/abc.pdf"
    assert body["hosted_invoice_url"] == "https://invoice.stripe.com/i/abc"


def test_invoice_pdf_cross_tenant_returns_404_not_403(env, monkeypatch):
    """An invoice whose `customer` doesn't match the caller's
    `stripe_customer_id` must return 404 — otherwise the endpoint
    is a customer-id enumeration oracle."""
    _seed_subscription_with_customer(env, customer_id="cus_acme")
    _install_fake_stripe(
        monkeypatch,
        invoice=_FakeInvoice(customer="cus_other_tenant"),
    )
    r = env["client"].get(
        "/v1/billing/invoices/in_test/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "invoice not found"


def test_invoice_pdf_unknown_invoice_id_returns_404(env, monkeypatch):
    """Stripe's InvalidRequestError (unknown ID, malformed) → 404."""
    _seed_subscription_with_customer(env, customer_id="cus_acme")
    _install_fake_stripe(monkeypatch, raise_invalid=True)
    r = env["client"].get(
        "/v1/billing/invoices/in_unknown/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 404


def test_invoice_pdf_upstream_stripe_error_returns_502(env, monkeypatch):
    """Network blip / Stripe outage → 502 so the frontend can
    distinguish a transient upstream failure from an unknown ID."""
    _seed_subscription_with_customer(env, customer_id="cus_acme")
    _install_fake_stripe(monkeypatch, raise_other=True)
    r = env["client"].get(
        "/v1/billing/invoices/in_test/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 502
    assert "Stripe" in r.json()["detail"]


def test_invoice_pdf_409_when_pdf_not_yet_finalised(env, monkeypatch):
    """Draft invoice with no `invoice_pdf` yet → 409 so the frontend
    knows to retry rather than redirect to a 404 page."""
    _seed_subscription_with_customer(env, customer_id="cus_acme")
    _install_fake_stripe(
        monkeypatch,
        invoice=_FakeInvoice(
            customer="cus_acme",
            invoice_pdf=None,
            hosted_invoice_url=None,
        ),
    )
    r = env["client"].get(
        "/v1/billing/invoices/in_draft/pdf",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 409
    assert "not yet available" in r.json()["detail"]


def test_free_tier_token_cap():
    assert billing_mod.free_tier_token_cap() == 100_000


def test_subscription_endpoint_does_not_require_admin(env):
    """Members should be able to see their own org's billing state."""
    r = env["client"].get(
        "/v1/billing/subscription", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200


# --- W8.2 token-cap helpers ----------------------------------------


def test_tier_token_cap_table():
    """Free has a finite cap; paid tiers are uncapped at this layer
    (per-run sandbox cost cap still applies)."""
    assert billing_mod.tier_token_cap("free") == 100_000
    assert billing_mod.tier_token_cap("team") is None
    assert billing_mod.tier_token_cap("business") is None


def test_tier_token_cap_unknown_falls_back_to_free():
    """Defensive: a misconfigured tier string must NOT grant business
    ceilings. Mirrors `sandbox_limits_for_tier`'s safe-default policy."""
    assert billing_mod.tier_token_cap("ultimate-platinum") == 100_000
    assert billing_mod.tier_token_cap("") == 100_000
    assert billing_mod.tier_token_cap(None) == 100_000


def test_current_period_token_usage_sums_runs(env):
    """Both `done` and `failed` runs count — the API call already
    happened and is on the operator's bill."""
    from claudestruct.server.models import Run, RunStatus
    factory = env["factory"]
    with factory() as session:
        org_id = session.query(Org).first().id
        user_id = session.query(User).first().id
        for status_val, in_t, out_t in [
            (RunStatus.done.value, 1000, 500),
            (RunStatus.done.value, 200, 100),
            (RunStatus.failed.value, 50, 25),
        ]:
            session.add(Run(
                run_id=f"r-{status_val}-{in_t}",
                org_id=org_id, user_id=user_id,
                status=status_val, task="dev", description="x",
                input_tokens=in_t, output_tokens=out_t,
            ))
        session.commit()
        used = billing_mod.current_period_token_usage(session, org_id)
    # 1000+500 + 200+100 + 50+25 = 1875 (failed run included)
    assert used == 1875


def test_current_period_token_usage_excludes_other_orgs(env):
    """Tenant isolation: org A's runs must not show up in org B's tally."""
    from claudestruct.server.models import Run, RunStatus
    factory = env["factory"]
    with factory() as session:
        org_a_id = session.query(Org).first().id
        # Build a second org with one user.
        org_b = Org(slug="other", name="Other")
        session.add(org_b)
        session.flush()
        user_b = User(email="root@other.test")
        session.add(user_b)
        session.flush()
        session.add(Membership(
            user_id=user_b.id, org_id=org_b.id, role=Role.member.value,
        ))
        session.add(Run(
            run_id="r-noisy-neighbour",
            org_id=org_b.id, user_id=user_b.id,
            status=RunStatus.done.value, task="dev", description="x",
            input_tokens=999_999, output_tokens=0,
        ))
        session.commit()
        used = billing_mod.current_period_token_usage(session, org_a_id)
    assert used == 0


def test_current_period_token_usage_excludes_runs_outside_window(env):
    """A row with `created_at` before period_start (e.g. an old run
    that was prune-eligible but kept) must NOT count toward the
    current-period total."""
    from claudestruct.server.models import Run, RunStatus
    factory = env["factory"]
    now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    with factory() as session:
        org_id = session.query(Org).first().id
        user_id = session.query(User).first().id
        # 50 days ago: outside the calendar-month default window.
        old = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
        session.add(Run(
            run_id="r-old", org_id=org_id, user_id=user_id,
            status=RunStatus.done.value, task="dev", description="x",
            input_tokens=10_000, output_tokens=10_000,
            created_at=old,
        ))
        # In-window: keep this one in.
        recent = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
        session.add(Run(
            run_id="r-new", org_id=org_id, user_id=user_id,
            status=RunStatus.done.value, task="dev", description="x",
            input_tokens=100, output_tokens=200,
            created_at=recent,
        ))
        session.commit()
        used = billing_mod.current_period_token_usage(session, org_id, now=now)
    assert used == 300
