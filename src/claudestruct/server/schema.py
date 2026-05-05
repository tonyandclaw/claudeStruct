"""Pydantic request/response shapes for the REST API.

Kept in one file because the surface is small. Each schema is the
public API contract — renaming fields here is a breaking change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# --- Health ---------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    # W8.5: data-residency tag. Mirrors the X-CS-Region response
    # header so a healthz probe doubles as a residency check.
    region: Optional[str] = None


# --- Auth / keys ----------------------------------------------------

class CreateKeyRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)


class KeyMetadata(BaseModel):
    key_id: str
    name: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]


class CreateKeyResponse(KeyMetadata):
    # Full secret is returned exactly once at creation; never again.
    full_key: str


class KeyList(BaseModel):
    keys: list[KeyMetadata]


# --- Dashboard ------------------------------------------------------

class RunRow(BaseModel):
    run_id: str
    started_at: Optional[str]
    ended_at: Optional[str]
    task: Optional[str]
    model: Optional[str]
    effort: Optional[str]
    reason: Optional[str]
    duration_ms: Optional[int]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    cache_warnings: list[str]


class DashboardResponse(BaseModel):
    runs: list[RunRow]


# --- Shared / team dashboard (W6.5) ---------------------------------

class AuthorRollup(BaseModel):
    """Per-author aggregate for the leaderboard."""

    user_id: int
    email: str
    runs: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


class TaskRollup(BaseModel):
    """Per-task aggregate for the donut chart."""

    task: str
    runs: int
    cost_usd: float


class TeamDashboardResponse(BaseModel):
    """Org-scoped rollup of recent runs.

    Authoritative numbers come from the `runs` table (W6.1) so legacy
    JSONL data isn't mixed in — the dashboard is meant to reflect what
    the daemon has actually executed for this org.
    """

    org_id: int
    org_slug: str
    total_runs: int
    total_cost_usd: float
    by_author: list[AuthorRollup]
    by_task: list[TaskRollup]
    recent: list[RunRow]


# --- Budget ---------------------------------------------------------

class BudgetResponse(BaseModel):
    spent_usd: float
    cap_usd: float
    warn_threshold_usd: float
    exceeded: bool
    near_limit: bool
    remaining_usd: float


# --- Runs (W6.1 will fill in execution semantics) -------------------

class CreateRunRequest(BaseModel):
    task: Literal["dev", "review", "plan", "debug"]
    description: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)
    max_bytes: Optional[int] = Field(default=None, ge=1)


class CreateRunResponse(BaseModel):
    run_id: str
    status: Literal["queued"]
    note: str = (
        "Run is queued. The daemon-mode worker (W6.1) picks it up and "
        "writes results back to the same row; poll GET /v1/runs/{id} "
        "for status. Run `cs serve worker` to drain the queue locally."
    )


class TokenCapExceededResponse(BaseModel):
    """Body returned with HTTP 402 when an org's monthly token cap
    would be breached by accepting another run.

    The fields let the caller render an actionable message ("you've
    used X of Y; resets at Z; upgrade for unlimited") without a
    second round-trip. ``failed`` runs count toward ``used`` because
    the Anthropic API call still happened — that's already on the
    operator's bill.
    """
    detail: str
    used_tokens: int
    cap_tokens: int
    period_end: datetime
    tier: str


class RunDetail(RunRow):
    pass


# --- Audit log (W8.4) ----------------------------------------------

class AuditHeadResponse(BaseModel):
    """Latest seq + entry_hash. seq=0 means the chain is empty and
    entry_hash is the genesis sentinel."""
    seq: int
    entry_hash: str


class AuditEntryResponse(BaseModel):
    seq: int
    action: str
    resource_type: str
    resource_id: str
    payload: Optional[object]
    actor_user_id: Optional[int]
    created_at: datetime
    prev_hash: str
    entry_hash: str


class AuditListResponse(BaseModel):
    entries: list[AuditEntryResponse]
    next_cursor_seq: Optional[int] = None


class AuditVerifyResponse(BaseModel):
    ok: bool
    total: int
    head_seq: Optional[int] = None
    head_hash: Optional[str] = None
    broken_at_seq: Optional[int] = None
    broken_reason: Optional[str] = None


# --- Billing (W8.2) ------------------------------------------------

class SandboxLimitsResponse(BaseModel):
    """Per-tier worker quotas surfaced via the subscription endpoint
    (W8.3). Lets the SPA / CLI render "you're 3/4 of your concurrent
    runs cap" without re-implementing the lookup table."""
    max_concurrent_runs: int
    max_runtime_seconds: int
    max_cost_usd: float


class SubscriptionResponse(BaseModel):
    """Org's current subscription state. ``tier`` defaults to "free"
    when no Subscription row exists yet."""
    org_slug: str
    tier: Literal["free", "team", "business"]
    status: Optional[str]  # mirrors Stripe: "active" / "past_due" / "canceled" / null
    stripe_customer_id: Optional[str]
    current_period_end: Optional[datetime]
    # W8.3: surface the active sandbox caps so callers don't have to
    # re-derive them from the tier name.
    sandbox_limits: SandboxLimitsResponse


class CheckoutRequest(BaseModel):
    tier: Literal["team", "business"]
    success_url: str = Field(min_length=8)
    cancel_url: str = Field(min_length=8)


class CheckoutResponse(BaseModel):
    """Stub: real Stripe Checkout integration ships when the merchant
    keys are available. The shape is pinned so the frontend can be
    written against it now."""
    checkout_session_id: str
    url: str
    note: str = (
        "Stripe checkout sessions are stubbed in the draft -- the route "
        "returns a deterministic placeholder URL until live merchant "
        "keys are provisioned."
    )


class UsageResponse(BaseModel):
    """Period-to-date token usage. Mirrors the budget endpoint shape
    so dashboards can render either with one renderer."""
    period_start: datetime
    period_end: datetime
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


# --- Cost-regression alerts (W6.5) ---------------------------------

class TaskBaselineResponse(BaseModel):
    """Per-task cost baseline for the org's lookback window. ``stddev``
    is ``None`` when the task has fewer than ``min_sample_size`` runs —
    the UI should render "insufficient data" rather than a noisy
    estimate."""
    task: str
    sample_size: int
    mean_cost_usd: float
    stddev_cost_usd: Optional[float]
    max_cost_usd: float


class RunAlertResponse(BaseModel):
    """A single anomalously-expensive run flagged against its task's
    baseline. ``z_score`` is comparable across tasks because each run
    is scored against its own task's distribution."""
    run_id: str
    task: str
    user_id: int
    user_email: Optional[str]
    cost_usd: float
    baseline_mean_usd: float
    baseline_stddev_usd: float
    z_score: float
    threshold_sigma: float
    created_at: datetime


class TeamAlertsResponse(BaseModel):
    """Org-scoped cost-anomaly alerts (W6.5).

    The snapshot is keyed off the caller's org so the leaderboard and
    alerts agree on what "this team" means. A separate notification
    delivery surface (Slack / email) consumes the same alert objects;
    that surface is still pending in the W6.5 roadmap entry."""
    generated_at: datetime
    lookback_days: int
    recent_window_hours: int
    sigma_threshold: float
    min_sample_size: int
    baselines: list[TaskBaselineResponse]
    alerts: list[RunAlertResponse]


# --- SLO snapshot (W8.7) -------------------------------------------

class SloWindow(BaseModel):
    """Per-window rollup. ``None`` percentiles signal an empty window
    so consumers can render "n/a" rather than a misleading 0."""
    window: Literal["24h", "7d", "30d"]
    total_runs: int
    succeeded: int
    failed: int
    success_rate: Optional[float]
    error_rate: Optional[float]
    p50_run_start_ms: Optional[int]
    p95_run_start_ms: Optional[int]
    p99_run_start_ms: Optional[int]
    p50_duration_ms: Optional[int]
    p95_duration_ms: Optional[int]
    p99_duration_ms: Optional[int]


class SloTargetsResponse(BaseModel):
    success_rate: float
    p95_run_start_ms: int
    p95_duration_ms: int


class SloSnapshotResponse(BaseModel):
    """Fleet-wide SLO snapshot powering the public status page."""
    generated_at: datetime
    targets: SloTargetsResponse
    windows: list[SloWindow]
