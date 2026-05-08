"""Cost-regression detection (W6.5).

Folds the ``runs`` table per org and emits ``Alert`` rows for runs
whose ``cost_usd`` is more than ``sigma`` standard deviations above
the org's mean over a rolling lookback window.

Why mean+stddev (and not p99 / median absolute deviation):
- One number per org keeps the daemon's working set small.
- 2σ matches the convention operators expect from "regression"
  alerts in tools like Datadog / Honeycomb.
- We exclude failed runs (cost is still recorded for them but the
  partial work isn't comparable) — a single crashed run shouldn't
  push the baseline up enough to mask a real regression next time.

Edge cases:
- Orgs with < 3 successful runs in the window have no statistical
  baseline; skipped silently. Two data points can't yield a
  meaningful stddev.
- Stddev = 0 (all costs identical) → any non-zero deviation is
  flagged. This is rare in practice; matches Datadog behaviour.
- Only runs from the last ``check_recent_hours`` (default 24h) are
  candidates — we don't want a single old expensive run to fire an
  alert every time the cron runs.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from claudestruct.server.models import Org, Run, RunStatus, Team, TeamMembership
from claudestruct.server.notify import Alert, Notifier

log = logging.getLogger("claudestruct.alerts")

DEFAULT_SIGMA = 2.0
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_CHECK_RECENT_HOURS = 24
MIN_BASELINE_RUNS = 3

# Thresholds for the team-budget burn alert (W6.5 follow-up). Match
# the existing severity ladder used by `finding_to_alert`: warning
# under cap, critical at/over.
TEAM_BUDGET_WARN_RATIO = 0.8
TEAM_BUDGET_CRIT_RATIO = 1.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CostRegressionFinding:
    """One run flagged as regressing relative to its org baseline.

    Kept separate from ``Alert`` so the producer logic is testable
    without coupling to the notifier wire format."""
    org_id: int
    org_slug: str
    run_id: str
    run_cost_usd: float
    baseline_mean: float
    baseline_stddev: float
    sigma_above: float


def _stddev(samples: list[float], mean: float) -> float:
    """Population stddev. We use population (not sample) because we're
    describing the org's actual historical spend, not estimating an
    unknown distribution from a sample."""
    if len(samples) <= 1:
        return 0.0
    var = sum((x - mean) ** 2 for x in samples) / len(samples)
    return math.sqrt(var)


def compute_cost_regression_alerts(
    session: Session,
    *,
    sigma: float = DEFAULT_SIGMA,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    check_recent_hours: int = DEFAULT_CHECK_RECENT_HOURS,
    now: datetime | None = None,
) -> list[CostRegressionFinding]:
    """Fold the ``runs`` table and return one finding per regressing run.

    Caller is responsible for dispatching findings to a ``Notifier``
    via ``dispatch_findings`` — keeps the producer pure for tests.
    """
    n = (now or _now_utc()).astimezone(timezone.utc)
    baseline_cutoff = n - timedelta(days=lookback_days)
    recent_cutoff = n - timedelta(hours=check_recent_hours)

    rows: list[Run] = list(session.execute(
        select(Run).where(
            Run.status == RunStatus.done.value,
            Run.created_at >= baseline_cutoff,
        )
    ).scalars())

    # Group by org. Map of org_id → list[Run].
    by_org: dict[int, list[Run]] = {}
    for r in rows:
        by_org.setdefault(r.org_id, []).append(r)

    findings: list[CostRegressionFinding] = []
    for org_id, org_runs in by_org.items():
        if len(org_runs) < MIN_BASELINE_RUNS:
            # Insufficient baseline; skip silently.
            continue

        # Baseline = ALL successful runs in lookback (including the
        # recent ones). Excluding the recent ones would double-count
        # them as "outliers vs the past" every cron tick — an outlier
        # then becomes part of next tick's baseline naturally.
        costs = [float(r.cost_usd or 0.0) for r in org_runs]
        mean = sum(costs) / len(costs)
        stddev = _stddev(costs, mean)

        # Resolve org slug once per org (single row lookup, cheap).
        org_slug = ""
        org = session.get(Org, org_id)
        if org is not None:
            org_slug = org.slug

        for r in org_runs:
            if r.created_at is None:
                continue
            created = r.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < recent_cutoff:
                continue
            cost = float(r.cost_usd or 0.0)
            # When stddev is 0, any deviation is "infinitely many σ" —
            # represent as a large finite value so downstream sorting
            # still works and the alert message is readable.
            if stddev == 0:
                if cost <= mean:
                    continue
                sigma_above = float("inf")
            else:
                sigma_above = (cost - mean) / stddev
            if sigma_above < sigma:
                continue
            findings.append(CostRegressionFinding(
                org_id=org_id,
                org_slug=org_slug,
                run_id=r.run_id,
                run_cost_usd=cost,
                baseline_mean=mean,
                baseline_stddev=stddev,
                sigma_above=sigma_above,
            ))

    # Stable sort — operator scanning a Slack channel sees the worst
    # regressions first. ``inf`` sorts to the top naturally.
    findings.sort(key=lambda f: f.sigma_above, reverse=True)
    return findings


def finding_to_alert(f: CostRegressionFinding) -> Alert:
    """Translate a producer finding into the wire-format ``Alert``.

    Severity ladder: ≥ 4σ is critical, ≥ 3σ is warning, otherwise info.
    Tuned so the default 2σ trigger threshold gives "info" rolls — the
    alert exists in the channel for trend-spotting but doesn't page
    anyone. Operators bump ``--sigma`` if they want fewer infos.
    """
    if f.sigma_above >= 4.0 or math.isinf(f.sigma_above):
        severity = "critical"
    elif f.sigma_above >= 3.0:
        severity = "warning"
    else:
        severity = "info"
    sigma_str = "∞" if math.isinf(f.sigma_above) else f"{f.sigma_above:.1f}"
    summary = (
        f"run {f.run_id} cost ${f.run_cost_usd:.2f} is {sigma_str}σ above "
        f"30d mean ${f.baseline_mean:.2f}"
    )
    return Alert(
        kind="cost_regression",
        severity=severity,
        org_slug=f.org_slug,
        summary=summary,
        details={
            "run_id": f.run_id,
            "run_cost_usd": round(f.run_cost_usd, 6),
            "baseline_mean_usd": round(f.baseline_mean, 6),
            "baseline_stddev_usd": round(f.baseline_stddev, 6),
            "sigma_above": (
                "inf" if math.isinf(f.sigma_above) else round(f.sigma_above, 3)
            ),
        },
    )


def dispatch_findings(
    findings: list[CostRegressionFinding],
    notifier: Notifier,
) -> int:
    """Convert findings → Alerts and hand each to the notifier.

    Returns the count dispatched so the CLI can echo "fired N alerts".
    The notifier's failure mode is its own concern — we don't catch
    here; ``LogNotifier`` can't fail and ``SlackWebhookNotifier``
    swallows HTTP errors itself with a log line.
    """
    count = 0
    for f in findings:
        notifier.notify(finding_to_alert(f))
        count += 1
    return count


# --- Team budget-cap rollup alerts (W6.5 follow-up) ----------------


@dataclass(frozen=True)
class TeamBudgetFinding:
    """One team flagged for having burned ≥ ``TEAM_BUDGET_WARN_RATIO``
    of the org's monthly USD cap. Rolled up over the org's current
    Stripe billing period (or UTC calendar month if Stripe state is
    absent).

    ``ratio`` = ``team_cost_usd`` / ``org_cap_usd``. ≥ 1.0 means the
    team alone has already met or exceeded the org's cap — at that
    point the org is presumably either over-budget or unable to
    submit new runs (whichever the W8.2 enforcement path decided).
    """
    org_id: int
    org_slug: str
    team_id: int
    team_slug: str
    team_cost_usd: float
    org_cap_usd: float
    ratio: float
    period_start: datetime
    period_end: datetime


def compute_team_budget_alerts(
    session: Session,
    *,
    now: datetime | None = None,
) -> list[TeamBudgetFinding]:
    """Roll up per-team spend in the current billing period and flag
    teams that have burned ≥ 80 % of the org's tier USD cap.

    Uncapped tiers (team / business have ``TIER_USD_CAPS = None``)
    are skipped — there's no fixed denominator to compare against.

    Empty teams (no ``team_memberships`` rows) are skipped — they can
    have no spend by definition; emitting a 0 / cap row would be noise.
    """
    # Local import to dodge a circular: billing -> alerts (via cli) -> billing.
    from claudestruct.server.billing import (
        current_period_bounds,
        get_or_default,
        tier_usd_cap,
    )

    n = (now or _now_utc()).astimezone(timezone.utc)
    findings: list[TeamBudgetFinding] = []

    for org in session.execute(select(Org)).scalars():
        sub = get_or_default(session, org.id)
        cap = tier_usd_cap(sub.tier)
        if cap is None or cap <= 0:
            continue  # uncapped tier, or 0-cap which we'd divide by below
        period_start, period_end = current_period_bounds(sub, now=n)
        teams = list(session.execute(
            select(Team).where(Team.org_id == org.id)
        ).scalars())
        if not teams:
            continue
        for team in teams:
            user_ids = {
                m.user_id for m in session.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team.id
                    )
                ).scalars()
            }
            if not user_ids:
                continue
            team_cost = float(session.execute(
                select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(
                    Run.org_id == org.id,
                    Run.user_id.in_(user_ids),
                    Run.created_at >= period_start,
                    Run.created_at < period_end,
                )
            ).scalar() or 0.0)
            ratio = team_cost / cap
            if ratio < TEAM_BUDGET_WARN_RATIO:
                continue
            findings.append(TeamBudgetFinding(
                org_id=org.id,
                org_slug=org.slug,
                team_id=team.id,
                team_slug=team.slug,
                team_cost_usd=team_cost,
                org_cap_usd=cap,
                ratio=ratio,
                period_start=period_start,
                period_end=period_end,
            ))
    # Highest ratio first — operator scanning a Slack channel sees
    # the most-over-budget team at the top.
    findings.sort(key=lambda f: f.ratio, reverse=True)
    return findings


def team_budget_finding_to_alert(f: TeamBudgetFinding) -> Alert:
    """Translate a team-budget finding into the wire-format Alert.

    Severity ladder mirrors `finding_to_alert`:
      * ratio ≥ 1.0 → critical (team alone has met or exceeded the
        org's monthly cap)
      * 0.8 ≤ ratio < 1.0 → warning
      * below 0.8 we don't emit a finding at all, so info-tier is
        unused here.
    """
    severity = "critical" if f.ratio >= TEAM_BUDGET_CRIT_RATIO else "warning"
    pct = round(f.ratio * 100, 1)
    summary = (
        f"team {f.team_slug} has burned {pct}% of {f.org_slug}'s "
        f"monthly cap (${f.team_cost_usd:.2f} / ${f.org_cap_usd:.2f})"
    )
    return Alert(
        kind="team_budget_burn",
        severity=severity,
        org_slug=f.org_slug,
        summary=summary,
        details={
            "team_slug": f.team_slug,
            "team_cost_usd": round(f.team_cost_usd, 6),
            "org_cap_usd": round(f.org_cap_usd, 6),
            "ratio": round(f.ratio, 3),
            "period_start": f.period_start.isoformat(),
            "period_end": f.period_end.isoformat(),
        },
    )


def dispatch_team_budget_findings(
    findings: list[TeamBudgetFinding],
    notifier: Notifier,
) -> int:
    """Convert team-budget findings → Alerts and dispatch each."""
    count = 0
    for f in findings:
        notifier.notify(team_budget_finding_to_alert(f))
        count += 1
    return count


# --- SLO error-budget burn-rate alerts (A.2) ------------------------
#
# Multi-window burn-rate detection per the Google SRE workbook. The
# error budget is ``1 - SUCCESS_RATE_TARGET``; the burn rate is
# ``observed_error_rate / target_error_rate``.
#
# Threshold rationale:
#   * Fast burn (≥ 14.4× over 1h) → at this rate the org would
#     consume its entire 30-day budget in ~2 hours. Page severity.
#   * Slow burn (≥ 6× over 6h) → 30-day budget consumed in 5 days
#     of sustained burn. Ticket severity.
#
# We only emit a finding when the relevant window has ≥ 10 terminal
# runs — under that, a single failure dominates the rate and the
# alert just yells "100% burn" every cron tick at low traffic.
TARGET_ERROR_RATE = 1.0 - 0.999  # mirrors SUCCESS_RATE_TARGET in slo.py
FAST_BURN_THRESHOLD = 14.4
SLOW_BURN_THRESHOLD = 6.0
BURN_RATE_MIN_RUNS = 10


@dataclass(frozen=True)
class BurnRateFinding:
    """One window flagged for burning the error budget too fast."""
    org_id: int
    org_slug: str
    window: str  # "1h" / "6h"
    window_seconds: int
    total_runs: int
    failed: int
    error_rate: float
    burn_rate: float        # observed / target
    threshold: float        # FAST_BURN_THRESHOLD or SLOW_BURN_THRESHOLD


def _compute_burn_for_window(
    session: Session,
    *,
    org_id: int,
    org_slug: str,
    now: datetime,
    window_seconds: int,
    window_label: str,
    threshold: float,
) -> BurnRateFinding | None:
    cutoff = now - timedelta(seconds=window_seconds)
    rows = list(session.execute(
        select(Run).where(
            Run.org_id == org_id,
            Run.status.in_((RunStatus.done.value, RunStatus.failed.value)),
            Run.created_at >= cutoff,
        )
    ).scalars())
    total = len(rows)
    if total < BURN_RATE_MIN_RUNS:
        return None
    failed = sum(1 for r in rows if r.status == RunStatus.failed.value)
    error_rate = failed / total
    burn_rate = error_rate / TARGET_ERROR_RATE if TARGET_ERROR_RATE else 0.0
    if burn_rate < threshold:
        return None
    return BurnRateFinding(
        org_id=org_id,
        org_slug=org_slug,
        window=window_label,
        window_seconds=window_seconds,
        total_runs=total,
        failed=failed,
        error_rate=error_rate,
        burn_rate=burn_rate,
        threshold=threshold,
    )


def compute_burn_rate_alerts(
    session: Session,
    *,
    now: datetime | None = None,
) -> list[BurnRateFinding]:
    """Per-org error-budget burn-rate detector.

    Two windows: 1h (fast — page) and 6h (slow — ticket). An org
    can produce findings for both windows in the same pass; the
    notifier dedupes downstream by `(kind, org, window)` if needed.

    Orgs with fewer than ``BURN_RATE_MIN_RUNS`` terminal runs in
    the window are skipped — at low traffic the rate is dominated
    by a single failure and the alert just yells "100% burn"
    every cron tick.
    """
    n = (now or _now_utc()).astimezone(timezone.utc)
    findings: list[BurnRateFinding] = []
    for org in session.execute(select(Org)).scalars():
        for label, secs, threshold in (
            ("1h", 60 * 60, FAST_BURN_THRESHOLD),
            ("6h", 6 * 60 * 60, SLOW_BURN_THRESHOLD),
        ):
            f = _compute_burn_for_window(
                session,
                org_id=org.id,
                org_slug=org.slug,
                now=n,
                window_seconds=secs,
                window_label=label,
                threshold=threshold,
            )
            if f is not None:
                findings.append(f)
    # Highest burn first — operator scanning a Slack channel sees the
    # most-on-fire org at the top.
    findings.sort(key=lambda f: f.burn_rate, reverse=True)
    return findings


def burn_rate_finding_to_alert(f: BurnRateFinding) -> Alert:
    """Translate a burn-rate finding into the wire-format ``Alert``.

    Severity: ``critical`` for the 1h fast-burn window (page),
    ``warning`` for the 6h slow-burn window (ticket).
    """
    severity = "critical" if f.threshold >= FAST_BURN_THRESHOLD else "warning"
    summary = (
        f"{f.org_slug} burning error budget at {f.burn_rate:.1f}× "
        f"target over {f.window} ({f.failed}/{f.total_runs} failed)"
    )
    return Alert(
        kind="slo_error_budget_burn",
        severity=severity,
        org_slug=f.org_slug,
        summary=summary,
        details={
            "window": f.window,
            "window_seconds": f.window_seconds,
            "total_runs": f.total_runs,
            "failed_runs": f.failed,
            "error_rate": round(f.error_rate, 6),
            "burn_rate": round(f.burn_rate, 3),
            "threshold": f.threshold,
        },
    )


def dispatch_burn_rate_findings(
    findings: list[BurnRateFinding],
    notifier: Notifier,
) -> int:
    """Convert burn-rate findings → Alerts and dispatch each."""
    count = 0
    for f in findings:
        notifier.notify(burn_rate_finding_to_alert(f))
        count += 1
    return count


def run_alert_pass(
    session_factory: Callable[[], Session],
    notifier: Notifier,
    *,
    sigma: float = DEFAULT_SIGMA,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    check_recent_hours: int = DEFAULT_CHECK_RECENT_HOURS,
) -> int:
    """Single compute → dispatch pass. Used by both the one-shot
    `cs serve alerts` command and the long-running scheduler.

    Runs three detector kinds in sequence:
      1. Cost-regression (per-run outliers vs org baseline)
      2. Team budget-cap burn (team's share of org's monthly cap)
      3. SLO error-budget burn-rate (Google SRE multi-window)

    Each pass opens its own session so we don't hold a transaction
    across the (potentially slow) network call into the notifier.
    Returns the total number of alerts dispatched across all kinds.
    """
    with session_factory() as session:
        cost_findings = compute_cost_regression_alerts(
            session,
            sigma=sigma,
            lookback_days=lookback_days,
            check_recent_hours=check_recent_hours,
        )
        team_findings = compute_team_budget_alerts(session)
        burn_findings = compute_burn_rate_alerts(session)
    n = dispatch_findings(cost_findings, notifier)
    n += dispatch_team_budget_findings(team_findings, notifier)
    n += dispatch_burn_rate_findings(burn_findings, notifier)
    return n


class AlertsScheduler:
    """Long-running daemon thread that recomputes cost-regression
    alerts on an interval (W6.5 follow-up).

    Mirrors ``WorkerThread`` from ``server/worker.py``: a
    ``threading.Event`` flag drives a loop that sleeps for
    ``interval_s`` between passes; ``stop()`` wakes the sleep so
    SIGINT/SIGTERM can shut down promptly without hard-killing an
    in-flight notifier call.

    The scheduler swallows per-pass exceptions and logs them — a
    transient DB blip or a bad notifier reply must not take the
    daemon down (cron would have just retried on the next tick;
    the scheduler keeps the same behaviour).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        notifier: Notifier,
        interval_s: float = 3600.0,
        sigma: float = DEFAULT_SIGMA,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        check_recent_hours: int = DEFAULT_CHECK_RECENT_HOURS,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self.session_factory = session_factory
        self.notifier = notifier
        self.interval_s = interval_s
        self.sigma = sigma
        self.lookback_days = lookback_days
        self.check_recent_hours = check_recent_hours
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Pass counter — exposed for tests so they can wait_until_passed.
        self.passes_completed = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._loop,
            name="claudestruct-alerts-scheduler",
            daemon=True,
        )
        self._thread = t
        t.start()

    def stop(self, timeout: float | None = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_once(self) -> int:
        """Synchronous single pass — useful for the `--once` CLI mode
        and as a hook tests can drive without spinning up a thread."""
        try:
            n = run_alert_pass(
                self.session_factory,
                self.notifier,
                sigma=self.sigma,
                lookback_days=self.lookback_days,
                check_recent_hours=self.check_recent_hours,
            )
        except Exception:  # noqa: BLE001
            log.exception("alerts scheduler pass crashed")
            return 0
        finally:
            self.passes_completed += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            # Use the event's wait so stop() wakes us.
            self._stop.wait(timeout=self.interval_s)
