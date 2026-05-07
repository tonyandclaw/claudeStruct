"""Tests for the W6.3 teams sub-item.

Teams are intra-org sub-groupings used for delegated admin and
per-team rollups (the dashboard's `--team <slug>` filter is a
follow-up of this work). The schema choice is deliberately a
separate `team_memberships` table — not a `team_id` column on the
flat `Membership` table — so a user can belong to multiple teams
within one org without duplicating their org membership row.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import (
    Membership,
    Org,
    Role,
    Team,
    TeamMembership,
    User,
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


def _seed_org_with_user(factory, *, org_slug: str, email: str) -> tuple[int, int]:
    """Create one org + one user + their membership. Returns
    (org_id, user_id) so callers can chain follow-up rows."""
    with factory() as s:
        org = Org(slug=org_slug, name=org_slug.title())
        user = User(email=email)
        s.add_all([org, user])
        s.flush()
        s.add(Membership(user_id=user.id, org_id=org.id, role=Role.member.value))
        s.commit()
        return org.id, user.id


# --- Schema ---------------------------------------------------------


def test_team_can_be_created_under_an_org(factory):
    org_id, _ = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        team = Team(org_id=org_id, slug="platform-eng", name="Platform Eng")
        s.add(team)
        s.commit()
        s.refresh(team)
        assert team.id is not None
        assert team.org_id == org_id
        assert team.created_at is not None


def test_team_slug_is_unique_within_an_org(factory):
    """Locking the (org_id, slug) UNIQUE constraint. The DB enforces
    it; if a future migration drops the constraint the test catches
    it before a duplicate-team RBAC bug ships."""
    org_id, _ = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        s.add(Team(org_id=org_id, slug="dup", name="A"))
        s.add(Team(org_id=org_id, slug="dup", name="B"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_team_slug_can_repeat_across_orgs(factory):
    """Two orgs each having a `platform-eng` team must NOT collide.
    The UNIQUE is keyed on (org_id, slug), not slug alone."""
    org_a, _ = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    org_b, _ = _seed_org_with_user(factory, org_slug="rival", email="u@r")
    with factory() as s:
        s.add(Team(org_id=org_a, slug="platform-eng", name="A"))
        s.add(Team(org_id=org_b, slug="platform-eng", name="B"))
        s.commit()  # no IntegrityError
        teams = s.execute(select(Team)).scalars().all()
        assert {t.org_id for t in teams} == {org_a, org_b}


def test_team_membership_links_user_to_team(factory):
    org_id, user_id = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        team = Team(org_id=org_id, slug="t", name="T")
        s.add(team)
        s.flush()
        s.add(TeamMembership(team_id=team.id, user_id=user_id))
        s.commit()
        rows = s.execute(select(TeamMembership)).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == team.id and rows[0].user_id == user_id


def test_user_cannot_be_added_to_same_team_twice(factory):
    """The (team_id, user_id) UNIQUE keeps a script that re-runs
    `add-team-member` from creating dupes. Lock the schema-level
    enforcement so the CLI's idempotency check isn't the only line
    of defence."""
    org_id, user_id = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        team = Team(org_id=org_id, slug="t", name="T")
        s.add(team)
        s.flush()
        s.add(TeamMembership(team_id=team.id, user_id=user_id))
        s.add(TeamMembership(team_id=team.id, user_id=user_id))
        with pytest.raises(IntegrityError):
            s.commit()


def test_user_can_belong_to_multiple_teams_in_one_org(factory):
    """The whole point of the separate-table design: a polyglot
    engineer in both `platform-eng` and `growth` should not
    duplicate their org membership."""
    org_id, user_id = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        a = Team(org_id=org_id, slug="platform-eng", name="P")
        b = Team(org_id=org_id, slug="growth", name="G")
        s.add_all([a, b])
        s.flush()
        s.add(TeamMembership(team_id=a.id, user_id=user_id))
        s.add(TeamMembership(team_id=b.id, user_id=user_id))
        s.commit()
        # Org membership row count should still be 1 — teams are
        # NOT a parallel membership system.
        assert (
            s.execute(select(Membership).where(Membership.user_id == user_id))
            .scalars().all().__len__() == 1
        )
        # But the user has 2 team memberships.
        assert (
            s.execute(
                select(TeamMembership).where(TeamMembership.user_id == user_id)
            ).scalars().all().__len__() == 2
        )


def test_deleting_team_cascades_to_team_memberships(factory):
    """`ondelete="CASCADE"` on the team_id FK means dropping a
    team cleans up its membership rows automatically. Without this
    a team rename via delete + re-add would leak orphan rows."""
    org_id, user_id = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        team = Team(org_id=org_id, slug="t", name="T")
        s.add(team)
        s.flush()
        s.add(TeamMembership(team_id=team.id, user_id=user_id))
        s.commit()
        # SQLite ondelete=CASCADE only fires when foreign_keys
        # pragma is on, which init_db enables on session init.
        # Sanity-check the row exists, then delete the team.
        assert s.execute(select(TeamMembership)).scalars().all()
        s.delete(team)
        s.commit()
        assert s.execute(select(TeamMembership)).scalars().all() == []


def test_deleting_org_cascades_through_teams(factory):
    """Drop-the-org should sweep up teams + their memberships in
    one go. Locks the cascade chain Org → Team → TeamMembership."""
    org_id, user_id = _seed_org_with_user(factory, org_slug="acme", email="u@a")
    with factory() as s:
        team = Team(org_id=org_id, slug="t", name="T")
        s.add(team)
        s.flush()
        s.add(TeamMembership(team_id=team.id, user_id=user_id))
        s.commit()
    with factory() as s:
        org = s.execute(select(Org).where(Org.id == org_id)).scalar_one()
        s.delete(org)
        s.commit()
        assert s.execute(select(Team)).scalars().all() == []
        assert s.execute(select(TeamMembership)).scalars().all() == []


# --- CLI: add-team ------------------------------------------------


def test_cli_add_team_creates_a_row(factory, monkeypatch, tmp_path):
    """`cs serve add-team <slug> <name> <org_slug>` should create
    a Team row + print the new id."""
    from click.testing import CliRunner

    from claudestruct.server.cli import serve_group

    # Seed the prerequisite org.
    with factory() as s:
        s.add(Org(slug="acme", name="Acme"))
        s.commit()

    db_url = "sqlite:///" + str(tmp_path / "shared.db")
    # Initialise the on-disk DB the same way the CLI will see it.
    from sqlalchemy import create_engine as _ce
    eng = _ce(db_url)
    init_db(eng)
    with make_session_factory(eng)() as s:
        s.add(Org(slug="acme", name="Acme"))
        s.commit()

    result = CliRunner().invoke(
        serve_group,
        ["add-team", "platform-eng", "Platform Eng", "acme",
         "--db-url", db_url],
    )
    assert result.exit_code == 0, result.output
    assert "created team" in result.output

    with make_session_factory(_ce(db_url))() as s:
        teams = s.execute(select(Team)).scalars().all()
        assert len(teams) == 1
        assert teams[0].slug == "platform-eng"


def test_cli_add_team_is_idempotent_on_re_run(tmp_path):
    """Re-running `add-team` with the same args should NOT 422 on
    the unique constraint — print the existing team id instead.
    Lets ops scripts run unconditionally."""
    from click.testing import CliRunner
    from sqlalchemy import create_engine as _ce

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "idem.db")
    eng = _ce(db_url)
    init_db(eng)
    with make_session_factory(eng)() as s:
        s.add(Org(slug="acme", name="Acme"))
        s.commit()

    runner = CliRunner()
    first = runner.invoke(
        serve_group,
        ["add-team", "t", "T", "acme", "--db-url", db_url],
    )
    second = runner.invoke(
        serve_group,
        ["add-team", "t", "T", "acme", "--db-url", db_url],
    )
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already exists" in second.output


def test_cli_add_team_rejects_unknown_org(tmp_path):
    from click.testing import CliRunner
    from sqlalchemy import create_engine as _ce

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "noorg.db")
    init_db(_ce(db_url))

    result = CliRunner().invoke(
        serve_group,
        ["add-team", "t", "T", "missing-org", "--db-url", db_url],
    )
    assert result.exit_code != 0
    assert "missing-org" in result.output


# --- CLI: add-team-member ----------------------------------------


def test_cli_add_team_member_happy_path(tmp_path):
    from click.testing import CliRunner
    from sqlalchemy import create_engine as _ce

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "tm.db")
    eng = _ce(db_url)
    init_db(eng)
    with make_session_factory(eng)() as s:
        org = Org(slug="acme", name="Acme")
        user = User(email="u@a")
        s.add_all([org, user])
        s.flush()
        s.add(Membership(user_id=user.id, org_id=org.id, role=Role.member.value))
        s.add(Team(org_id=org.id, slug="t", name="T"))
        s.commit()

    result = CliRunner().invoke(
        serve_group,
        ["add-team-member", "u@a", "t", "acme", "--db-url", db_url],
    )
    assert result.exit_code == 0, result.output
    assert "added user 'u@a'" in result.output
    with make_session_factory(_ce(db_url))() as s:
        rows = s.execute(select(TeamMembership)).scalars().all()
        assert len(rows) == 1


def test_cli_add_team_member_rejects_non_org_user(tmp_path):
    """Belt-and-braces: a user must already be an org member before
    landing on a team. Without this guard a typo could quietly
    grant cross-org access via a team-scoped query later."""
    from click.testing import CliRunner
    from sqlalchemy import create_engine as _ce

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "nomember.db")
    eng = _ce(db_url)
    init_db(eng)
    with make_session_factory(eng)() as s:
        org = Org(slug="acme", name="Acme")
        user = User(email="outsider@x")  # exists but not in the org
        s.add_all([org, user])
        s.flush()
        s.add(Team(org_id=org.id, slug="t", name="T"))
        s.commit()

    result = CliRunner().invoke(
        serve_group,
        ["add-team-member", "outsider@x", "t", "acme", "--db-url", db_url],
    )
    assert result.exit_code != 0
    assert "not in org" in result.output


def test_cli_add_team_member_idempotent(tmp_path):
    from click.testing import CliRunner
    from sqlalchemy import create_engine as _ce

    from claudestruct.server.cli import serve_group

    db_url = "sqlite:///" + str(tmp_path / "tm-idem.db")
    eng = _ce(db_url)
    init_db(eng)
    with make_session_factory(eng)() as s:
        org = Org(slug="acme", name="Acme")
        user = User(email="u@a")
        s.add_all([org, user])
        s.flush()
        s.add(Membership(user_id=user.id, org_id=org.id, role=Role.member.value))
        s.add(Team(org_id=org.id, slug="t", name="T"))
        s.commit()

    runner = CliRunner()
    first = runner.invoke(
        serve_group,
        ["add-team-member", "u@a", "t", "acme", "--db-url", db_url],
    )
    second = runner.invoke(
        serve_group,
        ["add-team-member", "u@a", "t", "acme", "--db-url", db_url],
    )
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already on team" in second.output
    with make_session_factory(_ce(db_url))() as s:
        assert (
            s.execute(select(TeamMembership)).scalars().all().__len__() == 1
        )
