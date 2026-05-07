"""``cs serve`` CLI subcommand group.

Entry point lazy-imports FastAPI/uvicorn so the lean install (no
``[server]`` extra) doesn't pay for them on cold start of the
non-server CLI commands.

Subcommands:
    cs serve run           -- launch the HTTP API
    cs serve init-db       -- create tables (idempotent)
    cs serve add-org       -- create an org
    cs serve add-user      -- create a user + membership
    cs serve add-key       -- mint an API key (printed once)
"""
from __future__ import annotations

from typing import Any

import click


def _ensure_server_deps() -> None:
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The `cs serve` subcommand needs the [server] extra:\n"
            "  pip install 'claudestruct[server]'"
        ) from exc


@click.group("serve", help="Daemon-mode HTTP API + RBAC (W6.2 + W6.3).")
def serve_group() -> None:
    pass


@serve_group.command("run", help="Launch the HTTP API via uvicorn.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind host. Use 0.0.0.0 to expose on the network.")
@click.option("--port", type=int, default=8787, show_default=True)
@click.option("--db-url", default=None,
              envvar="CLAUDESTRUCT_DATABASE_URL",
              help="SQLAlchemy URL. Default: sqlite:///./.claudestruct/server.db.")
@click.option("--run-root", type=click.Path(file_okay=False), default=".",
              show_default=True,
              help="Directory whose .claudestruct/runs/ feeds the dashboard.")
def serve_run(host: str, port: int, db_url: str | None, run_root: str) -> None:
    _ensure_server_deps()
    import uvicorn

    from claudestruct.server.app import create_app

    app = create_app(db_url=db_url, run_root=run_root)
    uvicorn.run(app, host=host, port=port, log_level="info")


@serve_group.command("init-db", help="Create tables (idempotent).")
@click.option("--db-url", default=None,
              envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_init_db(db_url: str | None) -> None:
    _ensure_server_deps()
    from claudestruct.server.db import init_db, make_engine

    engine = make_engine(db_url)
    init_db(engine)
    click.echo(f"initialized {engine.url}")


@serve_group.command("add-org", help="Create a new org.")
@click.argument("slug")
@click.argument("name")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_org(slug: str, name: str, db_url: str | None) -> None:
    _ensure_server_deps()
    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import Org

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = Org(slug=slug, name=name)
        session.add(org)
        session.commit()
        session.refresh(org)
        click.echo(f"created org id={org.id} slug={org.slug}")


@serve_group.command("add-user", help="Create a user + add to an org with a role.")
@click.argument("email")
@click.argument("org_slug")
@click.option("--name", default=None)
@click.option("--role", type=click.Choice(["admin", "member", "viewer"]),
              default="member", show_default=True)
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_user(email: str, org_slug: str, name: str | None, role: str,
                   db_url: str | None) -> None:
    _ensure_server_deps()
    from sqlalchemy import select

    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import Membership, Org, User

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one_or_none()
        if org is None:
            raise click.ClickException(f"org '{org_slug}' not found; run `cs serve add-org` first.")
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=email, name=name)
            session.add(user)
            session.flush()
        membership = session.execute(
            select(Membership).where(
                Membership.user_id == user.id, Membership.org_id == org.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            membership = Membership(user_id=user.id, org_id=org.id, role=role)
            session.add(membership)
        else:
            membership.role = role
        session.commit()
        click.echo(f"user id={user.id} email={user.email} role={role} org={org.slug}")


@serve_group.command(
    "add-team",
    help="Create a team within an org (W6.3 follow-up).",
)
@click.argument("slug")
@click.argument("name")
@click.argument("org_slug")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_team(
    slug: str, name: str, org_slug: str, db_url: str | None,
) -> None:
    """Idempotent on the (org_slug, slug) pair: re-running with the
    same args prints the existing team's id rather than 422-ing on
    the unique constraint. Lets ops scripts run unconditionally."""
    _ensure_server_deps()
    from sqlalchemy import select

    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import Org, Team

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one_or_none()
        if org is None:
            raise click.ClickException(
                f"org '{org_slug}' not found; run `cs serve add-org` first."
            )
        existing = session.execute(
            select(Team).where(Team.org_id == org.id, Team.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(
                f"team already exists id={existing.id} slug={existing.slug} "
                f"org={org_slug}"
            )
            return
        team = Team(org_id=org.id, slug=slug, name=name)
        session.add(team)
        session.commit()
        session.refresh(team)
        click.echo(
            f"created team id={team.id} slug={team.slug} org={org_slug}"
        )


@serve_group.command(
    "add-team-member",
    help="Add a user to a team within their org (W6.3 follow-up).",
)
@click.argument("email")
@click.argument("team_slug")
@click.argument("org_slug")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_team_member(
    email: str, team_slug: str, org_slug: str, db_url: str | None,
) -> None:
    """User must already belong to the org via `cs serve add-user`.
    A user joining a team they're not in the org of is rejected —
    teams are intra-org sub-groupings, not cross-org sharing.
    Idempotent on (team, user) — re-runs are no-ops."""
    _ensure_server_deps()
    from sqlalchemy import select

    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import (
        Membership,
        Org,
        Team,
        TeamMembership,
        User,
    )

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one_or_none()
        if org is None:
            raise click.ClickException(f"org '{org_slug}' not found.")
        team = session.execute(
            select(Team).where(Team.org_id == org.id, Team.slug == team_slug)
        ).scalar_one_or_none()
        if team is None:
            raise click.ClickException(
                f"team '{team_slug}' not found in org '{org_slug}'; "
                f"run `cs serve add-team {team_slug} '<name>' {org_slug}` first."
            )
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"user '{email}' not found.")
        # Belt-and-braces: user must already be in the org. Without
        # this check a typo could quietly grant cross-org access via
        # a team-scoped query later.
        membership = session.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.org_id == org.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise click.ClickException(
                f"user '{email}' is not in org '{org_slug}'; "
                f"run `cs serve add-user` first."
            )
        existing = session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team.id,
                TeamMembership.user_id == user.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(
                f"user '{email}' is already on team '{team_slug}'"
            )
            return
        session.add(TeamMembership(team_id=team.id, user_id=user.id))
        session.commit()
        click.echo(
            f"added user '{email}' to team '{team_slug}' in org '{org_slug}'"
        )


@serve_group.command("add-key", help="Mint an API key for a user in an org.")
@click.argument("email")
@click.argument("org_slug")
@click.option("--name", default=None, help="Human label (e.g. 'CI runner').")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_key(email: str, org_slug: str, name: str | None,
                  db_url: str | None) -> None:
    _ensure_server_deps()
    from sqlalchemy import select

    from claudestruct.server.auth import generate_key
    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import ApiKey, Membership, Org, User

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one_or_none()
        if org is None:
            raise click.ClickException(f"org '{org_slug}' not found.")
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"user '{email}' not found.")
        membership = session.execute(
            select(Membership).where(
                Membership.user_id == user.id, Membership.org_id == org.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise click.ClickException(
                f"user '{email}' is not a member of '{org_slug}'."
            )
        full_key, key_id, hashed = generate_key()
        row = ApiKey(
            user_id=user.id, org_id=org.id,
            key_id=key_id, hashed_secret=hashed, name=name,
        )
        session.add(row)
        session.commit()
        click.echo(f"key_id={key_id}")
        click.echo(f"full_key={full_key}")
        click.echo("Store the full_key now; it cannot be retrieved later.")


@serve_group.command("add-github-install",
                     help="Register a GitHub App installation (W6.6).")
@click.argument("installation_id", type=int)
@click.argument("org_slug")
@click.option("--secret", default=None,
              help="Webhook secret. Auto-generated if omitted; printed once.")
@click.option("--repo-filter", default=None,
              help="Optional case-insensitive substring filter on owner/name.")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
def serve_add_github_install(
    installation_id: int,
    org_slug: str,
    secret: str | None,
    repo_filter: str | None,
    db_url: str | None,
) -> None:
    """Map a GitHub App installation_id to a local org.

    Creates (or reuses) a sentinel `github-bot@<slug>.invalid` user
    that webhook-driven runs are attributed to, so the team
    dashboard's leaderboard surfaces them as a bot rather than
    crediting / blaming a real engineer.
    """
    _ensure_server_deps()
    import secrets as _secrets

    from sqlalchemy import select

    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.models import (
        GitHubInstallation,
        Membership,
        Org,
        Role,
        User,
    )

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one_or_none()
        if org is None:
            raise click.ClickException(f"org '{org_slug}' not found.")

        existing = session.execute(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == installation_id
            )
        ).scalar_one_or_none()
        if existing is not None and existing.is_active():
            raise click.ClickException(
                f"installation_id={installation_id} already registered for org_id={existing.org_id}"
            )

        bot_email = f"github-bot@{org_slug}.invalid"
        bot = session.execute(
            select(User).where(User.email == bot_email)
        ).scalar_one_or_none()
        if bot is None:
            bot = User(email=bot_email, name=f"GitHub bot ({org_slug})")
            session.add(bot)
            session.flush()
            session.add(Membership(
                user_id=bot.id, org_id=org.id, role=Role.member.value,
            ))

        webhook_secret = secret or _secrets.token_urlsafe(32)
        row = GitHubInstallation(
            installation_id=installation_id,
            org_id=org.id,
            webhook_secret=webhook_secret,
            repo_filter=repo_filter,
            bot_user_id=bot.id,
        )
        session.add(row)
        session.commit()
        click.echo(f"installation_id={installation_id} → org={org_slug}")
        click.echo(f"webhook_secret={webhook_secret}")
        click.echo("Paste this secret into the GitHub App settings as the Webhook secret.")


@serve_group.command("worker", help="Run the daemon-mode background worker (W6.1).")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
@click.option("--run-root", type=click.Path(file_okay=False), default=".",
              show_default=True)
@click.option("--once", is_flag=True,
              help="Drain the queue once and exit; useful for cron / batch.")
@click.option("--poll-interval", type=float, default=1.0, show_default=True,
              help="Seconds to sleep when the queue is empty (long-running mode).")
def serve_worker(db_url: str | None, run_root: str, once: bool,
                 poll_interval: float) -> None:
    """Drain queued runs.

    Without `--once`, runs forever as a daemon, blocking on the queue
    when empty. With `--once`, drains everything currently queued and
    exits — call from cron / a CI step / a Kubernetes Job.
    """
    _ensure_server_deps()
    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.worker import WorkerThread, drain_queue

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)

    if once:
        n = drain_queue(factory, run_root)
        click.echo(f"drained {n} run(s)")
        return

    thread = WorkerThread(
        run_root=run_root,
        session_factory=factory,
        poll_interval_s=poll_interval,
    )
    click.echo(f"worker started (poll {poll_interval}s); Ctrl-C to stop")
    thread.start()
    try:
        # Block the foreground until the user interrupts. The worker
        # thread is daemon so a hard kill won't leak it; the SIGINT
        # path below is for graceful drain.
        thread._thread.join() if thread._thread else None  # noqa: SLF001
    except KeyboardInterrupt:
        click.echo("\nstopping worker (will finish in-flight run)…")
        thread.stop()
        click.echo("worker stopped")


@serve_group.command("alerts")
@click.option("--db-url", default=None, envvar="CLAUDESTRUCT_DATABASE_URL")
@click.option("--sigma", type=float, default=2.0, show_default=True,
              help="Trigger threshold: cost > N standard deviations above org mean.")
@click.option("--lookback-days", type=int, default=30, show_default=True,
              help="Baseline window for the mean+stddev calculation.")
@click.option("--check-recent-hours", type=int, default=24, show_default=True,
              help="Window of recent runs to evaluate against the baseline.")
@click.option("--watch", is_flag=True, default=False,
              help="Run as a long-lived scheduler instead of one-shot. "
                   "Drains alerts every --interval seconds until SIGINT.")
@click.option("--interval", type=int, default=900, show_default=True,
              help="Seconds between passes when --watch is set (default 15min).")
@click.option("--max-iterations", type=int, default=0, show_default=True,
              help="Cap the watch loop at N passes (0 = unbounded). "
                   "Test seam — operators leave this at the default.")
def serve_alerts(db_url: str | None, sigma: float, lookback_days: int,
                 check_recent_hours: int, watch: bool, interval: int,
                 max_iterations: int) -> None:
    """Compute cost-regression alerts and dispatch via the notifier (W6.5).

    Provider is picked via ``CLAUDESTRUCT_NOTIFY_PROVIDER`` (``log`` by
    default; set to ``slack`` + ``CLAUDESTRUCT_SLACK_WEBHOOK_URL`` or
    ``email`` + ``CLAUDESTRUCT_SMTP_*`` for other channels). Findings
    flag runs whose cost is more than ``--sigma`` standard deviations
    above their org's 30-day mean successful-run cost.

    By default this is a one-shot pass — run from cron or a Kubernetes
    CronJob. Pass ``--watch`` to run as a long-lived daemon that polls
    every ``--interval`` seconds (suitable for systemd ``Type=simple``
    deployments next to ``cs serve run``).
    """
    _ensure_server_deps()
    from claudestruct.server.alerts import (
        compute_cost_regression_alerts,
        dispatch_findings,
    )
    from claudestruct.server.db import init_db, make_engine, make_session_factory
    from claudestruct.server.notify import default_notifier

    if interval <= 0:
        raise click.ClickException("--interval must be > 0")

    engine = make_engine(db_url)
    init_db(engine)
    factory = make_session_factory(engine)

    try:
        notifier = default_notifier()
    except RuntimeError as exc:
        # Misconfigured provider should be a loud, actionable failure
        # (cron job alerting on its own setup), not a silent skip.
        raise click.ClickException(str(exc)) from exc

    def _one_pass() -> int:
        with factory() as session:
            findings = compute_cost_regression_alerts(
                session,
                sigma=sigma,
                lookback_days=lookback_days,
                check_recent_hours=check_recent_hours,
            )
            return dispatch_findings(findings, notifier)

    if not watch:
        n = _one_pass()
        click.echo(f"alerts: {n} dispatched via {notifier.name}")
        return

    import time
    click.echo(
        f"alerts: watching every {interval}s via {notifier.name} "
        "(Ctrl-C to stop)"
    )
    iteration = 0
    try:
        while True:
            iteration += 1
            try:
                n = _one_pass()
                click.echo(f"alerts[{iteration}]: {n} dispatched")
            except Exception as exc:  # noqa: BLE001
                # A transient DB blip shouldn't tear the daemon down —
                # log + continue. Real misconfigs surface on the next
                # pass too.
                click.echo(f"alerts[{iteration}]: error: {exc}", err=True)
            if max_iterations and iteration >= max_iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nalerts: stopping watcher")


def attach_to(main: Any) -> None:
    """Mount the serve subcommand group on the top-level CLI. Called
    from ``claudestruct.cli`` so the import stays optional."""
    main.add_command(serve_group)
