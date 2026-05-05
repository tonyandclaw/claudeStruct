"""Cost-regression alerts (W6.5).

Surfaces runs that look anomalously expensive compared to the org's own
recent baseline, so a team lead can spot "someone burned $40 on a single
``cs plan``" without manually scrolling the team dashboard.

Design choices:

- **Per-task baselines.** A ``cs plan`` is naturally an order of
  magnitude more expensive than a ``cs review``. Pooling all tasks
  into one mean would either flag every plan (false positive) or hide
  outliers within each task (false negative). The baseline is computed
  separately per task; a run is flagged when its cost exceeds
  ``mean + sigma_threshold * stddev`` *for its own task type*.
- **Recent-window detection vs lookback baseline.** The baseline pulls
  ``lookback_days`` of history (default 30d) so trends settle. The
  evaluation window pulls only the most recent ``recent_window_hours``
  (default 72h) so the response surfaces fresh outliers, not last
  month's already-investigated incidents.
- **Sample-size floor.** With fewer than ``MIN_SAMPLE_SIZE`` runs in
  a task's baseline, stddev is unreliable. The task is reported in
  ``baselines`` with ``sample_size`` so a consumer can render
  "insufficient data", but no alerts fire from that task.
- **Org-scoped.** Cross-tenant baselines would leak spend signal
  between customers (e.g. "your plan costs are higher than the
  industry average") and aren't asked for. Every query filters by
  ``Run.org_id``.
- **Notification-channel-agnostic.** This module computes alerts;
  delivery (Slack webhook, email, SMTP) is a separate surface still
  marked pending in TODO.md. Shipping the detection lets the
  team-dashboard SPA render alerts today and lets ops stand up
  external pollers, while the notification path is designed.

The module is intentionally pure: ``compute_team_alerts`` takes a
session and parameters and returns dataclasses. The HTTP layer in
``routers/alerts.py`` translates those into pydantic responses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from claudestruct.server.models import Run, RunStatus, User

# --- Tunables -------------------------------------------------------

# Default lookback for the per-task baseline. 30 days is long enough
# to wash out daily noise without hiding seasonal regressions (e.g. a
# release week where every dev runs `cs plan` against the monorepo).
DEFAULT_LOOKBACK_DAYS = 30

# Window in which a run is "recent enough to alert on". Older outliers
# have presumably already been seen; surfacing them again clogs the
# alert feed.
DEFAULT_RECENT_WINDOW_HOURS = 72

# Z-score threshold for flagging. 2.0σ ≈ p97.5 under normality, which
# is generous enough that a run has to be visibly larger than the
# baseline to fire. 3.0σ is too strict for the noisy cost distribution
# we observe in practice.
DEFAULT_SIGMA_THRESHOLD = 2.0

# Below this many baseline samples per task, stddev is too noisy to
# trust. We surface the baseline as informational but don't fire
# alerts from it.
MIN_SAMPLE_SIZE = 5

# Hard cap on how many alerts the endpoint returns. Past 200 the UI
# is unusable and the operator needs to widen the lookback / fix the
# regression instead of paging through.
MAX_ALERTS = 200


# --- Datatypes ------------------------------------------------------

@dataclass(frozen=True)
class TaskBaseline:
    """Per-task cost baseline computed over the lookback window.

    ``stddev`` is None when ``sample_size`` is below
    :data:`MIN_SAMPLE_SIZE` — alerts derived from it would be noise."""
    task: str
    sample_size: int
    mean_cost_usd: float
    stddev_cost_usd: float | None
    max_cost_usd: float


@dataclass(frozen=True)
class RunAlert:
    """A single flagged run.

    ``z_score`` is computed on the same units as ``cost_usd`` (USD)
    and is comparable across tasks because each run is scored against
    its own task baseline.
    """
    run_id: str
    task: str
    user_id: int
    user_email: str | None
    cost_usd: float
    baseline_mean_usd: float
    baseline_stddev_usd: float
    z_score: float
    threshold_sigma: float
    created_at: datetime


@dataclass(frozen=True)
class TeamAlertsSnapshot:
    generated_at: datetime
    lookback_days: int
    recent_window_hours: int
    sigma_threshold: float
    min_sample_size: int
    baselines: list[TaskBaseline]
    alerts: list[RunAlert]


# --- Helpers --------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(ts: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; reattach UTC for safe math."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _mean_stddev(samples: list[float]) -> tuple[float, float]:
    """Population mean + (sample) stddev. Falls back to 0.0 stddev for
    n < 2 since the unbiased estimator is undefined there.

    We use the sample stddev (n-1 denominator) because the runs we see
    are themselves a sample from "all the runs the user might issue",
    not the entire population. The difference is small for n ≥ 30 but
    it's the principled choice."""
    n = len(samples)
    if n == 0:
        return 0.0, 0.0
    mean = sum(samples) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    return mean, math.sqrt(var)


# --- Computation ----------------------------------------------------

def compute_team_alerts(
    session: Session,
    *,
    org_id: int,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    recent_window_hours: int = DEFAULT_RECENT_WINDOW_HOURS,
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
    min_sample_size: int = MIN_SAMPLE_SIZE,
    now: datetime | None = None,
) -> TeamAlertsSnapshot:
    """Fold this org's recent runs into a list of cost-outlier alerts.

    Steps:
      1. Pull every ``done`` run for ``org_id`` whose ``created_at``
         falls in ``[now - lookback_days, now]``. Failed runs are
         excluded — a crashed run's cost reflects the partial work,
         not the operator's intent, and would distort the baseline.
      2. Group by task → compute mean / stddev per task.
      3. For each run in the recent eval window, score it against
         its task's baseline. Flag when ``z_score > sigma_threshold``
         and the baseline has ``≥ min_sample_size`` samples.
      4. Sort alerts by z_score desc, cap at :data:`MAX_ALERTS`.

    Notes:
      - The recent window is **inside** the baseline window. A
        spending burst in the last 72h will both inflate the baseline
        (slightly) and trigger the alert. That's fine for the typical
        case — the baseline is dominated by older, calmer history.
      - Negative ``cost_usd`` (rare but seen with billing-correction
        rows) is ignored. Cost-anomaly detection on negative values
        is meaningless.
    """
    n = (now or _now_utc()).astimezone(timezone.utc)
    base_cutoff = n - timedelta(days=lookback_days)
    eval_cutoff = n - timedelta(hours=recent_window_hours)

    rows: list[Run] = list(session.execute(
        select(Run).where(
            Run.org_id == org_id,
            Run.status == RunStatus.done.value,
            Run.created_at >= base_cutoff,
            Run.cost_usd >= 0,
        )
    ).scalars())

    # Per-task baseline -------------------------------------------------
    by_task: dict[str, list[float]] = {}
    for r in rows:
        by_task.setdefault(r.task, []).append(float(r.cost_usd))

    baselines: list[TaskBaseline] = []
    stats: dict[str, tuple[float, float]] = {}  # task -> (mean, stddev)
    for task, samples in by_task.items():
        mean, stddev = _mean_stddev(samples)
        stats[task] = (mean, stddev)
        baselines.append(TaskBaseline(
            task=task,
            sample_size=len(samples),
            mean_cost_usd=round(mean, 6),
            stddev_cost_usd=(
                round(stddev, 6) if len(samples) >= min_sample_size else None
            ),
            max_cost_usd=round(max(samples), 6),
        ))
    baselines.sort(key=lambda b: b.task)

    # Alert scoring -----------------------------------------------------
    # Materialise the user-id → email map up front so we don't fan out
    # per-row queries (n+1) when the alert list is long.
    user_ids = {r.user_id for r in rows}
    emails: dict[int, str] = {}
    if user_ids:
        emails = {
            u.id: u.email for u in session.execute(
                select(User).where(User.id.in_(user_ids))
            ).scalars()
        }

    alerts: list[RunAlert] = []
    for r in rows:
        created = _as_aware(r.created_at)
        if created < eval_cutoff:
            continue  # in baseline but outside the alert window
        samples = by_task.get(r.task, [])
        if len(samples) < min_sample_size:
            continue
        mean, stddev = stats[r.task]
        if stddev <= 0:
            # All runs identical; nothing to flag. (Avoid div-by-zero
            # and the degenerate "every run is infinitely anomalous"
            # outcome.)
            continue
        z = (float(r.cost_usd) - mean) / stddev
        if z <= sigma_threshold:
            continue
        alerts.append(RunAlert(
            run_id=r.run_id,
            task=r.task,
            user_id=r.user_id,
            user_email=emails.get(r.user_id),
            cost_usd=round(float(r.cost_usd), 6),
            baseline_mean_usd=round(mean, 6),
            baseline_stddev_usd=round(stddev, 6),
            z_score=round(z, 4),
            threshold_sigma=sigma_threshold,
            created_at=created,
        ))

    alerts.sort(key=lambda a: a.z_score, reverse=True)
    if len(alerts) > MAX_ALERTS:
        alerts = alerts[:MAX_ALERTS]

    return TeamAlertsSnapshot(
        generated_at=n,
        lookback_days=lookback_days,
        recent_window_hours=recent_window_hours,
        sigma_threshold=sigma_threshold,
        min_sample_size=min_sample_size,
        baselines=baselines,
        alerts=alerts,
    )
