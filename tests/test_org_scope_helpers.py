"""Tests for the C.5 tenant-isolation helpers.

`with_org_scope` and `require_org_owned` factor the manual
`Run.org_id == principal.org_id` pattern into a single import
site so a future router that forgets to filter is a missing
import (review-catchable) rather than a missing `.where(...)`
buried in a chained query.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from claudestruct.server import auth as auth_mod
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import ApiKey, Membership, Org, Role, User


@pytest.fixture()
def two_orgs():
    """Two seeded orgs with one user + one ApiKey each. Returns the
    pieces the tests need: factory + alice (org A) + bob (org B)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org_a = Org(slug="acme", name="Acme")
        org_b = Org(slug="beta", name="Beta")
        session.add_all([org_a, org_b])
        session.flush()
        alice = User(email="alice@acme")
        bob = User(email="bob@beta")
        session.add_all([alice, bob])
        session.flush()
        session.add(Membership(
            user_id=alice.id, org_id=org_a.id, role=Role.admin.value,
        ))
        session.add(Membership(
            user_id=bob.id, org_id=org_b.id, role=Role.admin.value,
        ))
        full_a, kid_a, hashed_a = auth_mod.generate_key()
        full_b, kid_b, hashed_b = auth_mod.generate_key()
        session.add(ApiKey(
            user_id=alice.id, org_id=org_a.id,
            key_id=kid_a, hashed_secret=hashed_a, name="alice-key",
        ))
        session.add(ApiKey(
            user_id=bob.id, org_id=org_b.id,
            key_id=kid_b, hashed_secret=hashed_b, name="bob-key",
        ))
        session.commit()

    return {
        "factory": factory,
        "alice_principal": auth_mod.Principal(
            user_id=1, user_email="alice@acme",
            org_id=1, org_slug="acme", role=Role.admin,
        ),
        "bob_principal": auth_mod.Principal(
            user_id=2, user_email="bob@beta",
            org_id=2, org_slug="beta", role=Role.admin,
        ),
    }


# --- with_org_scope --------------------------------------------------


def test_with_org_scope_filters_to_principal_org(two_orgs):
    """A SELECT scoped to alice (org_id=1) must only return rows
    from org A — no leakage."""
    with two_orgs["factory"]() as session:
        rows = session.execute(
            auth_mod.with_org_scope(
                select(ApiKey), ApiKey, two_orgs["alice_principal"],
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].name == "alice-key"


def test_with_org_scope_composes_with_other_where_clauses(two_orgs):
    """The helper must compose with caller-supplied filters — not
    replace them. A fluent `.where(...)` chain stays intact."""
    with two_orgs["factory"]() as session:
        # alice's key ANDed with name=alice-key still finds it.
        rows = session.execute(
            auth_mod.with_org_scope(
                select(ApiKey).where(ApiKey.name == "alice-key"),
                ApiKey, two_orgs["alice_principal"],
            )
        ).scalars().all()
    assert len(rows) == 1


def test_with_org_scope_does_not_leak_across_orgs(two_orgs):
    """Even with a query that names bob's key, scoping to alice's
    principal returns empty — the scope filter wins."""
    with two_orgs["factory"]() as session:
        rows = session.execute(
            auth_mod.with_org_scope(
                select(ApiKey).where(ApiKey.name == "bob-key"),
                ApiKey, two_orgs["alice_principal"],
            )
        ).scalars().all()
    assert rows == []


# --- require_org_owned -----------------------------------------------


def test_require_org_owned_passes_for_same_org(two_orgs):
    """Happy path: alice fetching her own key — the helper is silent."""
    with two_orgs["factory"]() as session:
        row = session.execute(
            select(ApiKey).where(ApiKey.name == "alice-key")
        ).scalar_one()
        # Must not raise.
        auth_mod.require_org_owned(
            row, two_orgs["alice_principal"], label="key",
        )


def test_require_org_owned_404_on_none(two_orgs):
    """A None row → 404 (not 500) so the helper can be the only
    fetch-site guard a router needs."""
    with pytest.raises(HTTPException) as exc:
        auth_mod.require_org_owned(
            None, two_orgs["alice_principal"], label="key",
        )
    assert exc.value.status_code == 404
    assert "key not found" in exc.value.detail


def test_require_org_owned_cross_tenant_returns_404_not_403(two_orgs):
    """The whole point of the 404 contract: a row that *exists* in
    another org must look identical to a row that doesn't exist
    anywhere. 403 would leak the existence of the id."""
    with two_orgs["factory"]() as session:
        bob_key = session.execute(
            select(ApiKey).where(ApiKey.name == "bob-key")
        ).scalar_one()
    with pytest.raises(HTTPException) as exc:
        auth_mod.require_org_owned(
            bob_key, two_orgs["alice_principal"], label="key",
        )
    assert exc.value.status_code == 404
    assert "key not found" in exc.value.detail
    # Critically, the helper does NOT raise 403.
    assert exc.value.status_code != 403


def test_require_org_owned_label_appears_in_detail():
    """`label` must surface in the error so the same helper produces
    sensible messages across ``key`` / ``team`` / ``invoice`` etc."""
    fake_principal = auth_mod.Principal(
        user_id=1, user_email="x", org_id=1, org_slug="x", role=Role.viewer,
    )
    for label in ("key", "team", "invoice", "skill"):
        with pytest.raises(HTTPException) as exc:
            auth_mod.require_org_owned(None, fake_principal, label=label)
        assert exc.value.detail == f"{label} not found"


def test_require_org_owned_500_on_non_tenant_scoped_model(two_orgs):
    """A row that has no `org_id` attribute is by definition not
    tenant-scoped — the helper is misapplied. Loud failure (500)
    rather than silent pass: silent pass would let a coding error
    slip through review."""

    class _NoOrg:
        # No `org_id` attribute.
        id = 42

    with pytest.raises(HTTPException) as exc:
        auth_mod.require_org_owned(
            _NoOrg(), two_orgs["alice_principal"], label="thing",
        )
    assert exc.value.status_code == 500
    assert "no org_id" in exc.value.detail


# --- End-to-end via /v1/keys ----------------------------------------


def test_keys_list_uses_helper_and_isolates_orgs(two_orgs):
    """The list-keys route was migrated to `with_org_scope` —
    verify that an alice key still only sees her own row, never
    bob's. Regression catch for the migration."""
    from fastapi.testclient import TestClient

    from claudestruct.server.app import create_app

    factory = two_orgs["factory"]
    # Mint a real bearer for alice.
    with factory() as session:
        alice_key = session.execute(
            select(ApiKey).where(ApiKey.name == "alice-key")
        ).scalar_one()
        # Generate a fresh full-key string for the auth path; we
        # need to build the ApiKey row's plaintext counterpart.
        full, key_id, hashed = auth_mod.generate_key()
        alice_key.key_id = key_id
        alice_key.hashed_secret = hashed
        session.commit()
    engine = factory().bind  # type: ignore[union-attr]
    # Re-use the existing engine so the test client sees the
    # pre-seeded data.
    app = create_app(engine=engine, run_root="/tmp", skip_init=True)
    client = TestClient(app)

    r = client.get(
        "/v1/keys",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert r.status_code == 200
    keys = r.json()["keys"]
    # Exactly one key visible — alice's.
    assert len(keys) == 1
    assert keys[0]["name"] == "alice-key"
