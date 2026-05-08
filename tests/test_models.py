"""Schema-constraint tests for the W6.3 model layer (C.2).

`test_teams.py` already exercises the Team / TeamMembership
constraints at length. This file fills in the remaining
constraint-level invariants on `Org` / `User` / `ApiKey` / `Run`
/ `UserSession` that the ORM enforces but no other test pins
against silent regression.

Schema-level tests are cheap and catch real defects: a future
PR that drops a UNIQUE constraint, flips a NOT NULL to
nullable, or renames a column while forgetting to ship the
migration would fail one of these.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import (
    ApiKey,
    Membership,
    Org,
    Role,
    Run,
    RunStatus,
    User,
    UserSession,
)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return make_session_factory(engine)


# --- Org -----------------------------------------------------------


def test_org_slug_is_unique(factory):
    """Two orgs cannot share a slug (it's the URL identifier; the
    UNIQUE constraint is the only thing stopping a tenant
    enumeration oracle via `?slug=` lookups)."""
    with factory() as session:
        session.add(Org(slug="acme", name="Acme"))
        session.commit()
    with factory() as session:
        session.add(Org(slug="acme", name="Acme dup"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_org_name_is_required(factory):
    with factory() as session:
        session.add(Org(slug="x", name=None))  # type: ignore[arg-type]
        with pytest.raises(IntegrityError):
            session.commit()


# --- User ----------------------------------------------------------


def test_user_email_is_unique(factory):
    with factory() as session:
        session.add(User(email="alice@example.com"))
        session.commit()
    with factory() as session:
        session.add(User(email="alice@example.com"))
        with pytest.raises(IntegrityError):
            session.commit()


# --- Membership ---------------------------------------------------


def test_membership_role_persists(factory):
    """`Membership.role` is the RBAC anchor; verify it round-trips
    through the DB. (Note: there's no `(user_id, org_id)` UNIQUE
    constraint on Membership today — adding one is a follow-up so
    role-escalation by duplicate-row insert can't slip through.)"""
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        u = User(email="alice@x")
        session.add_all([org, u])
        session.flush()
        session.add(Membership(user_id=u.id, org_id=org.id, role=Role.member.value))
        session.commit()
    with factory() as session:
        m = session.query(Membership).one()
        assert m.role == "member"


# --- ApiKey -------------------------------------------------------


def test_apikey_key_id_unique(factory):
    """`key_id` is the user-visible 16-char prefix; collisions
    would let one bearer impersonate another."""
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        u = User(email="alice@x")
        session.add_all([org, u])
        session.flush()
        session.add(ApiKey(
            user_id=u.id, org_id=org.id, key_id="dup_id",
            hashed_secret="h1", name="a",
        ))
        session.commit()
    with factory() as session:
        u = session.query(User).first()
        org = session.query(Org).first()
        session.add(ApiKey(
            user_id=u.id, org_id=org.id, key_id="dup_id",
            hashed_secret="h2", name="b",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_apikey_is_active_method(factory):
    """`ApiKey.is_active()` is the single read-site for "should this
    key auth a request?" Pin its semantics: `revoked_at is None`.
    It's a regular method (not a property) — call site difference
    matters because the auth path uses it in conditionals."""
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        u = User(email="alice@x")
        session.add_all([org, u])
        session.flush()
        live = ApiKey(
            user_id=u.id, org_id=org.id, key_id="live",
            hashed_secret="h", name="a",
        )
        revoked = ApiKey(
            user_id=u.id, org_id=org.id, key_id="dead",
            hashed_secret="h", name="b",
            revoked_at=datetime.now(timezone.utc),
        )
        session.add_all([live, revoked])
        session.commit()
        assert live.is_active() is True
        assert revoked.is_active() is False


# --- Run ----------------------------------------------------------


def test_run_run_id_unique(factory):
    """`run_id` is the public identifier; collisions would route a
    GET /v1/runs/{id} response to the wrong row across orgs."""
    with factory() as session:
        org = Org(slug="acme", name="Acme")
        u = User(email="alice@x")
        session.add_all([org, u])
        session.flush()
        session.add(Run(
            run_id="dup-run", org_id=org.id, user_id=u.id,
            status=RunStatus.queued.value, task="dev", description="x",
        ))
        session.commit()
    with factory() as session:
        org = session.query(Org).first()
        u = session.query(User).first()
        session.add(Run(
            run_id="dup-run", org_id=org.id, user_id=u.id,
            status=RunStatus.queued.value, task="dev", description="y",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_run_idempotency_key_unique_per_org(factory):
    """`(org_id, idempotency_key)` UNIQUE — same key in two
    different orgs is fine; same key twice in one org collides."""
    with factory() as session:
        org_a = Org(slug="acme", name="A")
        org_b = Org(slug="beta", name="B")
        ua = User(email="a@a")
        ub = User(email="b@b")
        session.add_all([org_a, org_b, ua, ub])
        session.flush()
        session.add(Run(
            run_id="r-a", org_id=org_a.id, user_id=ua.id,
            status=RunStatus.queued.value, task="dev", description="x",
            idempotency_key="shared",
        ))
        session.add(Run(
            run_id="r-b", org_id=org_b.id, user_id=ub.id,
            status=RunStatus.queued.value, task="dev", description="x",
            idempotency_key="shared",
        ))
        # Cross-org duplicate is fine.
        session.commit()

    with factory() as session:
        org_a = session.query(Org).filter_by(slug="acme").one()
        ua = session.query(User).filter_by(email="a@a").one()
        session.add(Run(
            run_id="r-a-dup", org_id=org_a.id, user_id=ua.id,
            status=RunStatus.queued.value, task="dev", description="x",
            idempotency_key="shared",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_run_idempotency_key_can_be_null_for_many_rows(factory):
    """NULL idempotency_key must NOT collide — the column is
    optional; SQLite + Postgres both treat NULL as 'distinct from
    any other NULL' for UNIQUE purposes (which is the SQL
    standard). Pin it so a future `nullable=False` change can't
    silently break callers that don't supply the header."""
    with factory() as session:
        org = Org(slug="acme", name="A")
        u = User(email="a@a")
        session.add_all([org, u])
        session.flush()
        for i in range(3):
            session.add(Run(
                run_id=f"r-{i}", org_id=org.id, user_id=u.id,
                status=RunStatus.queued.value, task="dev",
                description="x", idempotency_key=None,
            ))
        # Three NULLs in the same org is fine.
        session.commit()


# --- UserSession --------------------------------------------------


def test_user_session_token_unique(factory):
    """The session token is the cookie value — a duplicate would
    let one cookie authenticate two principals."""
    with factory() as session:
        org = Org(slug="acme", name="A")
        u = User(email="a@a")
        session.add_all([org, u])
        session.flush()
        expires = datetime.now(timezone.utc) + timedelta(days=14)
        session.add(UserSession(
            user_id=u.id, org_id=org.id,
            session_token="dup-token", provider="github",
            expires_at=expires,
        ))
        session.commit()
    with factory() as session:
        u = session.query(User).first()
        org = session.query(Org).first()
        expires = datetime.now(timezone.utc) + timedelta(days=14)
        session.add(UserSession(
            user_id=u.id, org_id=org.id,
            session_token="dup-token", provider="github",
            expires_at=expires,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_user_session_provider_field_persists(factory):
    """`provider` is read by audit + analytics; a refactor that
    drops the column would break the auth.login.success /
    auth.logout payloads."""
    with factory() as session:
        org = Org(slug="acme", name="A")
        u = User(email="a@a")
        session.add_all([org, u])
        session.flush()
        sess = UserSession(
            user_id=u.id, org_id=org.id,
            session_token="t", provider="google",
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(sess)
        session.commit()
        assert sess.provider == "google"
