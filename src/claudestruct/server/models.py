"""Multi-tenant RBAC schema (W6.3).

Minimal shape for the draft:

- ``Org`` — billing + isolation boundary. Every run, key, and membership
  is org-scoped.
- ``User`` — global identity (one row per email). A user can belong to
  multiple orgs via ``Membership``.
- ``Membership`` — (user, org) pair plus a role: ``admin``,
  ``member``, ``viewer``. Roles are checked at request time by
  ``auth.require_role``.
- ``ApiKey`` — opaque bearer token used by the CLI / CI. Stored as a
  SHA-256 of the secret so a DB compromise doesn't leak live keys.
  ``key_id`` is the user-visible prefix (e.g. ``ck_live_abcd``).

Future tables (W6.1+, not in this draft):
- ``Run`` — replaces the JSONL run logs once the daemon owns execution.
- ``AuditLog`` — append-only chain for W8.4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from claudestruct.server.db import Base


class Role(str, Enum):
    """Membership roles. String values match the DB column for simple
    comparisons in middleware."""

    admin = "admin"     # manage org, users, keys
    member = "member"   # submit runs, read dashboards
    viewer = "viewer"   # read-only


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="org", cascade="all, delete-orphan"
    )
    # Teams (W6.3 follow-up). Cascade so deleting an org cleans up
    # its teams + their memberships in one step. SQLite doesn't
    # enforce ondelete=CASCADE without the foreign_keys pragma, so
    # we rely on the ORM-level cascade for deterministic behaviour.
    teams: Mapped[list[Team]] = relationship(
        cascade="all, delete-orphan",
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.member.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)

    user: Mapped[User] = relationship(back_populates="memberships")
    org: Mapped[Org] = relationship(back_populates="memberships")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    hashed_secret: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")

    def is_active(self) -> bool:
        return self.revoked_at is None


# --- Teams (W6.3 follow-up) ----------------------------------------
#
# `Membership` is flat: a user belongs to an org with a role. Teams
# add a sub-grouping inside the org for delegated admin and per-team
# rollups (e.g. dashboard filtered to "platform-eng" runs).
#
# Schema choice: separate `teams` + `team_memberships` tables rather
# than a `team_id` column on `Membership` so a user can belong to
# multiple teams within the same org without duplicating their org
# membership row. The org membership stays the source of truth for
# RBAC; teams are organisational metadata + a future filter axis.


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        # A team's slug must be unique within an org but two orgs
        # can each have a `platform-eng` team without colliding.
        UniqueConstraint("org_id", "slug", name="uq_team_org_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), index=True,
    )
    slug: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc,
    )

    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team", cascade="all, delete-orphan",
    )


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        # A user can only appear once on a given team.
        UniqueConstraint("team_id", "user_id", name="uq_team_member"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc,
    )

    team: Mapped[Team] = relationship(back_populates="memberships")


class RunStatus(str, Enum):
    """Run state machine. Linear progression queued → running → done|failed.

    `done` covers normal completion; `failed` is reserved for exceptions
    that the worker caught (API error, timeout, etc.) so the API can
    surface a useful message to the requester."""

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Run(Base):
    """A daemon-mode submission (W6.1).

    Runs land here via `POST /v1/runs` with `status="queued"`. A worker
    picks one up, sets `status="running"`, executes
    `runner.run_task_and_log` synchronously, then writes the final
    cost/token numbers + `status="done"` (or `"failed"` with `error`).

    The run's JSONL event log still lives at the dashboard's existing
    `<run_root>/.claudestruct/runs/<run_id>.jsonl` location; the DB row
    is the queryable index, the JSONL file is the streaming detail.
    """

    __tablename__ = "runs"
    __table_args__ = (
        # Idempotency-Key is unique per org so two orgs can each
        # use the same opaque key without colliding. Nullable so
        # callers that don't supply the header still create rows.
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_run_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.queued.value, index=True)
    # Optional idempotency key from the `Idempotency-Key` request
    # header on POST /v1/runs. Scoped per-org via the UNIQUE
    # constraint above; resubmitting the same (org, key) returns
    # the original run instead of creating a duplicate.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True,
    )

    # Request shape — kept on the row for audit + replay even after
    # the JSONL log is purged.
    task: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(8192))
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    paths_json: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Outcomes — populated by the worker on success/failure.
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    # Timestamp indexes: SLO + dashboard + alert detectors all
    # filter by these. Indexed so the rolling-window queries
    # (`Run.created_at >= cutoff`) don't scan the whole table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    # W6.6 verdict-on-completion: when a run was enqueued by the
    # GitHub App webhook, these track which PR to post the verdict
    # back to. NULL on runs created via the REST API or CLI — those
    # have no upstream PR to comment on.
    github_installation_id: Mapped[int | None] = mapped_column(nullable=True)
    github_repo_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    github_pr_number: Mapped[int | None] = mapped_column(nullable=True)
    # W6.6 Checks API: the head commit SHA that the check-run reports
    # against. Captured from `pull_request.head.sha` on PR events;
    # NULL for issue_comment-triggered runs (those use the comment
    # path only — fetching the SHA needs a separate API call).
    github_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    github_check_run_id: Mapped[int | None] = mapped_column(nullable=True)
    # W6.6 PR-open: the default branch name captured at webhook time
    # so the worker knows which branch to fork from when opening a PR.
    github_base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserSession(Base):
    """OAuth-derived browser session (W6.4).

    Issued by the OAuth callback after a successful provider exchange;
    surfaced to the browser as a `claudestruct_session` HTTPOnly cookie.
    The cookie value is the random `session_token` (NOT the row id), so
    even DB read access doesn't directly leak active sessions through
    side-channels like log lines that mention `id=42`.

    `expires_at` is a hard cap independent of the cookie's `Max-Age`
    so a stolen cookie still rolls over within a bounded window.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    session_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32))  # "github", "google", ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_active(self, *, now: datetime | None = None) -> bool:
        # SQLite strips tzinfo on round-trip, so we may get a naive
        # datetime back from the DB even though we wrote a tz-aware
        # one. Force-attach UTC to make the comparison total-orderable
        # against `now`, which we always produce as tz-aware.
        ts = now or _now_utc()
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return self.revoked_at is None and ts < exp


class GitHubInstallation(Base):
    """GitHub App install ↔ org mapping (W6.6).

    A GitHub App is installed per-org / per-repo on GitHub's side; we
    receive webhooks tagged with an `installation.id` integer. To
    enqueue runs against the right tenant we need a row pinning that
    integer to one of *our* orgs plus the secret we'll use to verify
    incoming HMACs.

    The `webhook_secret` is what the user types into the GitHub App
    settings page; we compare against it on every request via
    constant-time `hmac.compare_digest`. Stored plaintext (not
    hashed) because verification needs the original bytes — protect
    via the usual at-rest controls (DB encryption, role separation
    on the host).

    `repo_filter` (optional) limits which repos in the install can
    trigger runs. Empty = all repos in the installation. Matched as
    case-insensitive substring against `<owner>/<name>`.
    """

    __tablename__ = "github_installations"

    id: Mapped[int] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    webhook_secret: Mapped[str] = mapped_column(String(128))
    repo_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_active(self) -> bool:
        return self.revoked_at is None
