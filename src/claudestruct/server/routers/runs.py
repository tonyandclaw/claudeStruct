"""Run submission + retrieval (W6.1).

``POST /v1/runs`` enqueues a Run row in the DB; the worker (see
``server/worker.py``) picks it up. ``GET /v1/runs/{id}`` reads the
DB first, falling back to the JSONL log so historical runs (created
before W6.1 landed) remain accessible.

Optional ``Idempotency-Key`` header on POST: a client can resubmit
a request safely (e.g. after a network blip) by sending the same
opaque string a second time — we'll return the *original* run's
response without creating a duplicate. Scoped per-org via the
``(org_id, idempotency_key)`` UNIQUE constraint on the Run table.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from claudestruct import dashboard as dash_mod
from claudestruct.server import audit as audit_mod
from claudestruct.server import auth as auth_mod
from claudestruct.server import billing as billing_mod
from claudestruct.server.models import Role, Run, RunStatus
from claudestruct.server.schema import (
    CreateRunRequest,
    CreateRunResponse,
    RunDetail,
    TokenCapExceededResponse,
)

router = APIRouter(prefix="/v1/runs", tags=["runs"])


def _enforce_token_cap(session: Session, *, org_id: int) -> None:
    """Reject the submit if the org has already burned its monthly cap.

    Reads the org's tier (defaulting to free when no Subscription row
    exists), looks up the cap, sums current-period token usage, and
    raises 402 with a structured body when the cap is met or exceeded.

    No-op for tiers where ``tier_token_cap`` returns ``None`` (team /
    business). Defensive on unknown tiers — they fall back to free
    semantics so a misconfigured row can't grant business ceilings.
    """
    sub = billing_mod.get_or_default(session, org_id)
    cap = billing_mod.tier_token_cap(sub.tier)
    if cap is None:
        return
    used = billing_mod.current_period_token_usage(session, org_id)
    if used < cap:
        return
    _, period_end = billing_mod.current_period_bounds(sub)
    payload = TokenCapExceededResponse(
        detail=(
            f"monthly token cap reached: {used} of {cap} tokens used "
            f"on the {sub.tier} tier; resets at {period_end.isoformat()}"
        ),
        used_tokens=used,
        cap_tokens=cap,
        period_end=period_end,
        tier=sub.tier,
    )
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=payload.model_dump(mode="json"),
    )


@router.post(
    "",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_run(
    body: CreateRunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.member)),
    session: Session = Depends(auth_mod.get_session),
) -> CreateRunResponse:
    """Enqueue a run. Returns 202 immediately; the worker picks it up
    and runs `runner.run_task_and_log` against Anthropic. Poll
    GET /v1/runs/{id} to follow progress.

    Also writes a `run.submit` row to the per-org audit chain (W8.4)
    so subsequent investigations can correlate "who submitted what" by
    chain seq.

    W8.2: rejects with 402 Payment Required when the caller's org has
    already burned its monthly token cap. The 402 body lists usage,
    cap, period_end, and tier so the SPA / CLI can render a single
    actionable upgrade prompt without a follow-up call.

    Idempotency: optional ``Idempotency-Key`` request header. When
    set, a second submit with the same `(org_id, key)` returns the
    original run's identifier instead of creating a duplicate — the
    standard pattern for safely retrying after a network blip. Scoped
    per-org via the UNIQUE constraint on `Run.idempotency_key`.
    """
    _enforce_token_cap(session, org_id=principal.org_id)

    # If the caller supplied an idempotency key, look up an existing
    # submission BEFORE writing a new row. Empty / whitespace-only
    # keys are treated as absent so a client can't accidentally
    # collide every run with the empty string. Both the lookup and
    # the row write use the same normalised value (None for empty)
    # so the UNIQUE constraint never sees an empty string.
    idem = idempotency_key.strip() if idempotency_key else None
    if idem == "":
        idem = None
    if idem:
        existing = session.execute(
            select(Run).where(
                Run.org_id == principal.org_id,
                Run.idempotency_key == idem,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Replay the original response. Status reflects current
            # state (the worker may have already moved it forward),
            # so the caller gets useful information rather than a
            # stale "queued".
            return CreateRunResponse(
                run_id=existing.run_id,
                status=existing.status,
                note="idempotent replay",
            )

    # 8 hex bytes → 16 chars; collision-resistant within the per-org
    # key space and short enough for log lines / URL paths.
    run_id = f"run-{secrets.token_hex(8)}"
    row = Run(
        run_id=run_id,
        org_id=principal.org_id,
        user_id=principal.user_id,
        status=RunStatus.queued.value,
        task=body.task,
        description=body.description,
        model=body.model,
        effort=body.effort,
        paths_json=json.dumps(body.paths) if body.paths else None,
        idempotency_key=idem,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        # Race: another concurrent request inserted the same
        # (org_id, idempotency_key) between our SELECT above and
        # this INSERT. Roll back, re-fetch, and replay the original.
        session.rollback()
        if idem:
            existing = session.execute(
                select(Run).where(
                    Run.org_id == principal.org_id,
                    Run.idempotency_key == idem,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return CreateRunResponse(
                    run_id=existing.run_id,
                    status=existing.status,
                    note="idempotent replay (race)",
                )
        raise
    audit_mod.record(
        session,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        action="run.submit",
        resource_type="run",
        resource_id=run_id,
        payload={
            "task": body.task,
            "model": body.model,
            "effort": body.effort,
            "max_tokens": body.max_tokens,
            "max_bytes": body.max_bytes,
        },
    )
    session.commit()
    return CreateRunResponse(run_id=run_id, status="queued")


def _row_to_detail(run: Run) -> RunDetail:
    """Translate a DB Run row into the public RunDetail shape."""
    return RunDetail(
        run_id=run.run_id,
        started_at=run.started_at.isoformat() if run.started_at else None,
        ended_at=run.ended_at.isoformat() if run.ended_at else None,
        task=run.task,
        model=run.model,
        effort=run.effort,
        reason=run.status,
        duration_ms=run.duration_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cache_creation_tokens=run.cache_creation_tokens,
        cost_usd=round(run.cost_usd, 6),
        cache_warnings=[run.error] if run.error else [],
    )


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    run_id: str,
    request: Request,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
) -> RunDetail:
    factory = request.app.state.session_factory
    with factory() as session:
        row: Run | None = session.execute(
            select(Run).where(Run.run_id == run_id)
        ).scalar_one_or_none()
        if row is not None:
            # Tenant isolation: a viewer in org A must not be able to
            # peek at org B's runs even by guessing the run_id.
            if row.org_id != principal.org_id:
                raise HTTPException(status_code=404, detail="run not found")
            return _row_to_detail(row)

    # Pre-W6.1 runs only live in the JSONL log. The fallback keeps
    # GET /v1/runs/{id} useful immediately after upgrade. Accessible
    # to any authenticated viewer in any org because the JSONL store
    # has no tenant column — acceptable while the legacy path is
    # phased out; tracked under W6.5 retention follow-up.
    root = Path(request.app.state.run_root)
    summaries = dash_mod.load_summaries(root)
    for s in summaries:
        if s.run_id == run_id:
            _ = principal  # explicit: the viewer is authenticated, that's enough here
            return RunDetail(
                run_id=s.run_id,
                started_at=s.started_at,
                ended_at=s.ended_at,
                task=s.task,
                model=s.model,
                effort=s.effort,
                reason=s.reason,
                duration_ms=s.duration_ms,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                cache_read_tokens=s.cache_read_tokens,
                cache_creation_tokens=s.cache_creation_tokens,
                cost_usd=round(s.cost_usd, 6),
                cache_warnings=s.cache_warnings,
            )
    raise HTTPException(status_code=404, detail="run not found")
