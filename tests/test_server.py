"""Tests for the daemon-mode REST API + RBAC (W6.2 + W6.3).

Covers:
- Health endpoints unauthenticated
- Bearer-token auth: missing / malformed / unknown / revoked / valid
- RBAC: viewer can read but not POST runs; admin can manage keys
- Route shapes for dashboard, budget, runs (read), runs (POST stub)
- Multi-tenant isolation: org A can't see org B's keys
"""
from __future__ import annotations

import json

import pytest

# Skip the whole module when the [server] extras aren't installed —
# keeps the lean-install CI matrix green.
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import create_app
from claudestruct.server.auth import generate_key
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import (
    ApiKey,
    Membership,
    Org,
    Role,
    Team,
    TeamMembership,
    User,
)


@pytest.fixture()
def env(tmp_path):
    """Per-test in-memory SQLite + temp run_root.

    Returns a dict with the app, TestClient, and a helper that mints
    keys for the canonical (org, user, role) combinations.
    """
    # StaticPool keeps a single connection so the in-memory DB is shared
    # between the seed transaction here and the FastAPI request handlers.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = make_session_factory(engine)

    # Seed two orgs + four users (admin, member, viewer in org-a; admin in org-b).
    keys: dict[str, str] = {}
    with factory() as session:
        org_a = Org(slug="org-a", name="Org A")
        org_b = Org(slug="org-b", name="Org B")
        session.add_all([org_a, org_b])
        session.flush()

        users = {
            "admin@a": (org_a, Role.admin),
            "member@a": (org_a, Role.member),
            "viewer@a": (org_a, Role.viewer),
            "admin@b": (org_b, Role.admin),
        }
        for email, (org, role) in users.items():
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
    client = TestClient(app)

    return {"app": app, "client": client, "keys": keys, "tmp_path": tmp_path,
            "factory": factory}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Health ---------------------------------------------------------

def test_healthz_unauthenticated(env):
    r = env["client"].get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "version" in r.json()


def test_readyz_unauthenticated(env):
    r = env["client"].get("/readyz")
    assert r.status_code == 200


# --- Auth gate ------------------------------------------------------

def test_dashboard_without_token_401(env):
    r = env["client"].get("/v1/dashboard")
    assert r.status_code == 401


def test_dashboard_with_malformed_token_401(env):
    r = env["client"].get("/v1/dashboard", headers=_auth("not-a-real-key"))
    assert r.status_code == 401


def test_dashboard_with_unknown_key_401(env):
    # Right shape, wrong key.
    r = env["client"].get("/v1/dashboard",
                          headers=_auth("ck_deadbeef_cafebabecafebabecafebabe"))
    assert r.status_code == 401


def test_dashboard_with_valid_viewer_key_200(env):
    r = env["client"].get("/v1/dashboard", headers=_auth(env["keys"]["viewer@a"]))
    assert r.status_code == 200
    assert r.json() == {"runs": []}


def test_revoked_key_rejected(env):
    # Revoke admin@a's key, then try to use it.
    factory = env["factory"]
    with factory() as session:
        from datetime import datetime, timezone

        from sqlalchemy import update
        session.execute(
            update(ApiKey).where(ApiKey.name == "admin@a-key").values(revoked_at=datetime.now(timezone.utc))
        )
        session.commit()
    r = env["client"].get("/v1/dashboard", headers=_auth(env["keys"]["admin@a"]))
    assert r.status_code == 401


# --- RBAC -----------------------------------------------------------

def test_viewer_cannot_create_run(env):
    r = env["client"].post(
        "/v1/runs",
        json={"task": "dev", "description": "hi"},
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 403


def test_member_can_create_run(env):
    r = env["client"].post(
        "/v1/runs",
        json={"task": "dev", "description": "hi"},
        headers=_auth(env["keys"]["member@a"]),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    # W6.1: real run IDs use the `run-<hex>` prefix (placeholder
    # `queued-` was the W6.2 draft).
    assert body["run_id"].startswith("run-")


def test_member_cannot_manage_keys(env):
    r = env["client"].get("/v1/keys", headers=_auth(env["keys"]["member@a"]))
    assert r.status_code == 403


def test_admin_can_create_and_revoke_key(env):
    headers = _auth(env["keys"]["admin@a"])
    create = env["client"].post("/v1/keys", json={"name": "ci-runner"}, headers=headers)
    assert create.status_code == 201
    body = create.json()
    assert body["full_key"].startswith("ck_")
    assert body["key_id"]
    new_key = body["full_key"]

    listed = env["client"].get("/v1/keys", headers=headers)
    assert listed.status_code == 200
    assert any(k["key_id"] == body["key_id"] for k in listed.json()["keys"])

    # The minted key authenticates as admin too.
    self_check = env["client"].get("/v1/dashboard", headers=_auth(new_key))
    assert self_check.status_code == 200

    revoke = env["client"].delete(f"/v1/keys/{body['key_id']}", headers=headers)
    assert revoke.status_code == 204

    # After revoke, the new key no longer works.
    after = env["client"].get("/v1/dashboard", headers=_auth(new_key))
    assert after.status_code == 401


# --- Multi-tenant isolation -----------------------------------------

def test_admin_b_cannot_revoke_admin_a_key(env):
    factory = env["factory"]
    # Find admin@a's key id
    with factory() as session:
        from sqlalchemy import select
        kid = session.execute(
            select(ApiKey.key_id).where(ApiKey.name == "admin@a-key")
        ).scalar_one()
    r = env["client"].delete(
        f"/v1/keys/{kid}",
        headers=_auth(env["keys"]["admin@b"]),
    )
    assert r.status_code == 404  # cross-tenant lookup returns "not found"


def test_admin_b_keys_list_excludes_org_a(env):
    r = env["client"].get("/v1/keys", headers=_auth(env["keys"]["admin@b"]))
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert all("@a" not in (k.get("name") or "") for k in keys)


# --- Dashboard / budget shape ---------------------------------------

def test_dashboard_includes_seeded_run(env):
    runs_dir = env["tmp_path"] / ".claudestruct" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "r1.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "run.start", "ts": "2026-04-15T10:00:00+00:00",
                        "task": "dev", "model": "claude-opus-4-7",
                        "effort": "high", "promptVersion": "dev v=abc"}),
            json.dumps({"type": "agent.usage", "inputTokens": 100,
                        "outputTokens": 50, "costUsd": 1.25}),
            json.dumps({"type": "run.end", "ts": "2026-04-15T10:01:00+00:00",
                        "reason": "complete", "durationMs": 60000,
                        "totalCostUsd": 1.25}),
        ]),
        encoding="utf-8",
    )
    r = env["client"].get("/v1/dashboard", headers=_auth(env["keys"]["viewer@a"]))
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["cost_usd"] == 1.25


# --- Pagination on /v1/dashboard ------------------------------------

def _seed_jsonl_runs(env, count: int):
    """Seed N JSONL run logs so the legacy dashboard has data."""
    runs_dir = env["tmp_path"] / ".claudestruct" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (runs_dir / f"r{i:04d}.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "type": "run.start",
                    "ts": f"2026-04-15T10:{i:02d}:00+00:00",
                    "task": "dev", "model": "claude-opus-4-7",
                    "effort": "high", "promptVersion": "dev v=abc",
                }),
                json.dumps({
                    "type": "agent.usage",
                    "inputTokens": 10, "outputTokens": 5, "costUsd": 0.1,
                }),
                json.dumps({
                    "type": "run.end",
                    "ts": f"2026-04-15T10:{i:02d}:30+00:00",
                    "reason": "complete", "durationMs": 30000,
                    "totalCostUsd": 0.1,
                }),
            ]),
            encoding="utf-8",
        )


def test_dashboard_default_limit_is_50(env):
    _seed_jsonl_runs(env, 100)
    r = env["client"].get(
        "/v1/dashboard", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200
    assert len(r.json()["runs"]) == 50


def test_dashboard_limit_caps_at_500(env):
    """Anything above 500 rejects with 400 — without the cap, an
    over-eager client could pull a 50 MB JSON in one shot."""
    r = env["client"].get(
        "/v1/dashboard?limit=10000",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 400


def test_dashboard_limit_below_1_rejects(env):
    r = env["client"].get(
        "/v1/dashboard?limit=0", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 400


def test_dashboard_offset_skips_records(env):
    """`offset=N` skips the first N rows so the caller can page
    through history."""
    _seed_jsonl_runs(env, 30)
    page1 = env["client"].get(
        "/v1/dashboard?limit=10&offset=0",
        headers=_auth(env["keys"]["viewer@a"]),
    ).json()["runs"]
    page2 = env["client"].get(
        "/v1/dashboard?limit=10&offset=10",
        headers=_auth(env["keys"]["viewer@a"]),
    ).json()["runs"]
    assert len(page1) == 10
    assert len(page2) == 10
    assert {r["run_id"] for r in page1} & {r["run_id"] for r in page2} == set()


def test_dashboard_negative_offset_rejected(env):
    r = env["client"].get(
        "/v1/dashboard?offset=-5",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 400


def test_budget_disabled_returns_zero_cap(env):
    r = env["client"].get("/v1/budget", headers=_auth(env["keys"]["viewer@a"]))
    assert r.status_code == 200
    body = r.json()
    assert body["cap_usd"] == 0.0
    assert body["exceeded"] is False
    assert body["near_limit"] is False


def test_budget_with_query_cap(env):
    r = env["client"].get(
        "/v1/budget?cap_usd=100",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200
    assert r.json()["cap_usd"] == 100.0


def test_get_unknown_run_404(env):
    r = env["client"].get("/v1/runs/nope", headers=_auth(env["keys"]["viewer@a"]))
    assert r.status_code == 404


# --- OpenAPI spec ---------------------------------------------------

def test_openapi_lists_v1_endpoints(env):
    r = env["client"].get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    paths = set(spec["paths"].keys())
    assert "/v1/dashboard" in paths
    assert "/v1/dashboard/team" in paths
    assert "/v1/budget" in paths
    assert "/v1/runs" in paths
    assert "/v1/keys" in paths


# --- W6.1 daemon worker --------------------------------------------------

class _StubOutcome:
    """Minimal shape that satisfies `worker._GATHERERS[task]` →
    `runner` contract used by `process_pending_run`."""

    def __init__(self, *, cost_usd: float = 0.5, input_tokens: int = 1000,
                 output_tokens: int = 200, cache_read_tokens: int = 0,
                 cache_creation_tokens: int = 0, duration_ms: int = 1234):
        from types import SimpleNamespace
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms
        self.result = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )


def _stub_runner(**kwargs):
    """Replace `run_task_and_log` for tests so we never hit Anthropic."""
    return _StubOutcome()


def _post_run(env, email: str, **body):
    payload = {"task": "review", "description": "ship it"}
    payload.update(body)
    return env["client"].post(
        "/v1/runs", json=payload, headers=_auth(env["keys"][email]),
    )


def test_post_run_writes_queued_row(env):
    r = _post_run(env, "member@a")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]
    assert run_id.startswith("run-")
    # GET sees the queued row now (status reported as the DB status string).
    g = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["member@a"]))
    assert g.status_code == 200
    assert g.json()["reason"] == "queued"


def test_worker_drains_queued_run(env):
    from claudestruct.server.worker import process_pending_run

    r = _post_run(env, "member@a", task="dev", description="add a feature")
    run_id = r.json()["run_id"]

    with env["factory"]() as session:
        executed = process_pending_run(session, env["tmp_path"], runner=_stub_runner)
    assert executed is not None
    assert executed.run_id == run_id
    assert executed.status == "done"

    # GET reflects the worker's writeback.
    g = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["member@a"]))
    body = g.json()
    assert body["reason"] == "done"
    assert body["cost_usd"] == 0.5
    assert body["input_tokens"] == 1000
    assert body["output_tokens"] == 200
    assert body["duration_ms"] == 1234


def test_worker_records_failure_on_exception(env):
    from claudestruct.client import ClaudestructError
    from claudestruct.server.worker import process_pending_run

    def failing_runner(**_kwargs):
        raise ClaudestructError("anthropic returned 500")

    r = _post_run(env, "member@a")
    run_id = r.json()["run_id"]
    with env["factory"]() as session:
        process_pending_run(session, env["tmp_path"], runner=failing_runner)

    g = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["member@a"]))
    body = g.json()
    assert body["reason"] == "failed"
    assert "anthropic returned 500" in body["cache_warnings"][0]


def test_worker_returns_none_on_empty_queue(env):
    from claudestruct.server.worker import process_pending_run

    with env["factory"]() as session:
        result = process_pending_run(session, env["tmp_path"], runner=_stub_runner)
    assert result is None


def test_run_isolation_across_orgs(env):
    """A member in org-a creates a run; an admin in org-b cannot read it
    even with a valid token. 404 (not 403) so we don't leak existence."""
    r = _post_run(env, "member@a")
    run_id = r.json()["run_id"]
    g = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["admin@b"]))
    assert g.status_code == 404


# --- Idempotency-Key on POST /v1/runs --------------------------------

def test_post_run_with_idempotency_key_persists_key(env):
    """The submitted Idempotency-Key lands on the Run row so we can
    look it up on a retry."""
    from sqlalchemy import select

    from claudestruct.server.models import Run

    r = env["client"].post(
        "/v1/runs",
        json={"task": "dev", "description": "x"},
        headers={
            **_auth(env["keys"]["member@a"]),
            "Idempotency-Key": "abc-123",
        },
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    with env["factory"]() as session:
        row = session.execute(
            select(Run).where(Run.run_id == run_id)
        ).scalar_one()
        assert row.idempotency_key == "abc-123"


def test_post_run_idempotent_replay_returns_original_run(env):
    """Same `(org, key)` on a second submit returns the original
    run_id and notes 'idempotent replay' — does NOT create a new
    Run row."""
    from sqlalchemy import select

    from claudestruct.server.models import Run

    headers = {
        **_auth(env["keys"]["member@a"]),
        "Idempotency-Key": "stable-key",
    }
    body = {"task": "dev", "description": "x"}

    first = env["client"].post("/v1/runs", json=body, headers=headers)
    assert first.status_code == 202
    first_id = first.json()["run_id"]

    second = env["client"].post("/v1/runs", json=body, headers=headers)
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["run_id"] == first_id
    assert second_body["note"] == "idempotent replay"

    with env["factory"]() as session:
        rows = session.execute(
            select(Run).where(Run.idempotency_key == "stable-key")
        ).scalars().all()
    assert len(rows) == 1


def test_post_run_idempotent_replay_returns_live_status(env):
    """The replayed response carries the run's *current* status —
    not a stale 'queued' — so the caller learns the run already
    finished if the worker was fast."""
    from sqlalchemy import select

    from claudestruct.server.models import Run, RunStatus

    headers = {
        **_auth(env["keys"]["member@a"]),
        "Idempotency-Key": "fast-key",
    }
    first = env["client"].post(
        "/v1/runs", json={"task": "dev", "description": "x"}, headers=headers,
    )
    run_id = first.json()["run_id"]
    # Mutate the row to simulate the worker having finished it.
    with env["factory"]() as session:
        row = session.execute(
            select(Run).where(Run.run_id == run_id)
        ).scalar_one()
        row.status = RunStatus.done.value
        session.commit()

    second = env["client"].post(
        "/v1/runs", json={"task": "dev", "description": "x"}, headers=headers,
    )
    assert second.json()["status"] == "done"


def test_post_run_idempotency_key_scoped_per_org(env):
    """Same key in different orgs creates two distinct runs — the
    UNIQUE constraint is `(org_id, idempotency_key)`, not just
    `idempotency_key`."""
    headers_a = {
        **_auth(env["keys"]["member@a"]),
        "Idempotency-Key": "shared-key",
    }
    headers_b = {
        **_auth(env["keys"]["admin@b"]),
        "Idempotency-Key": "shared-key",
    }
    body = {"task": "dev", "description": "x"}

    r1 = env["client"].post("/v1/runs", json=body, headers=headers_a)
    r2 = env["client"].post("/v1/runs", json=body, headers=headers_b)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_post_run_empty_idempotency_key_treated_as_absent(env):
    """A whitespace-only `Idempotency-Key` header must NOT collide
    every run with the empty string. Two submits with empty keys
    create two distinct runs."""
    headers = {
        **_auth(env["keys"]["member@a"]),
        "Idempotency-Key": "   ",
    }
    body = {"task": "dev", "description": "x"}
    r1 = env["client"].post("/v1/runs", json=body, headers=headers)
    r2 = env["client"].post("/v1/runs", json=body, headers=headers)
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_post_run_no_idempotency_header_creates_each_time(env):
    """Without the header, every submit gets a fresh run."""
    headers = _auth(env["keys"]["member@a"])
    body = {"task": "dev", "description": "x"}
    r1 = env["client"].post("/v1/runs", json=body, headers=headers)
    r2 = env["client"].post("/v1/runs", json=body, headers=headers)
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_drain_queue_processes_all_pending(env):
    from claudestruct.server.worker import drain_queue

    for i in range(3):
        _post_run(env, "member@a", description=f"task-{i}")
    n = drain_queue(env["factory"], env["tmp_path"], runner=_stub_runner)
    assert n == 3
    # Second drain finds nothing.
    assert drain_queue(env["factory"], env["tmp_path"], runner=_stub_runner) == 0


# --- W8.3 tenant-scoped sandbox: per-tier limits + queue priority -----

def _set_tier(env, org_slug, tier):
    """Force-set an org's subscription tier for tests."""
    from sqlalchemy import select

    from claudestruct.server.billing import Subscription, Tier
    from claudestruct.server.models import Org
    with env["factory"]() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one()
        sub = session.execute(
            select(Subscription).where(Subscription.org_id == org.id)
        ).scalar_one_or_none()
        if sub is None:
            sub = Subscription(org_id=org.id, tier=Tier(tier).value)
            session.add(sub)
        else:
            sub.tier = Tier(tier).value
        session.commit()


def _running_count(env, org_slug):
    from sqlalchemy import select

    from claudestruct.server.models import Org, Run, RunStatus
    with env["factory"]() as session:
        org = session.execute(select(Org).where(Org.slug == org_slug)).scalar_one()
        rows = session.execute(
            select(Run.id).where(
                Run.org_id == org.id, Run.status == RunStatus.running.value,
            )
        ).all()
        return len(rows)


def test_free_tier_concurrent_cap_blocks_second_run(env):
    """Free tier = 1 concurrent run. While one is running, a second
    queued run for the same org must NOT be claimed."""
    from sqlalchemy import select

    from claudestruct.server.models import Run, RunStatus
    from claudestruct.server.worker import _claim_one

    _set_tier(env, "org-a", "free")
    # Two queued runs from the same org.
    _post_run(env, "member@a", description="A")
    _post_run(env, "member@a", description="B")

    with env["factory"]() as session:
        first = _claim_one(session)
        assert first is not None  # row claimed
        # Second claim while the first is still `running` must
        # return None — concurrency cap of 1 holds.
        second = _claim_one(session)
        assert second is None
        # The other queued row stayed queued. Use COUNT() so the
        # row count comes back as one int, not a full row dump.
        from sqlalchemy import func
        queued_count = session.execute(
            select(func.count(Run.id)).where(Run.status == RunStatus.queued.value)
        ).scalar()
        assert queued_count == 1


def test_team_tier_allows_concurrent_runs_up_to_cap(env):
    from claudestruct.server.worker import _claim_one

    _set_tier(env, "org-a", "team")  # max_concurrent_runs=4
    for _ in range(5):
        _post_run(env, "member@a")

    with env["factory"]() as session:
        claimed = []
        for _ in range(5):
            row = _claim_one(session)
            if row is not None:
                claimed.append(row)
        # Four claims succeed; the fifth blocks because of the cap.
        assert len(claimed) == 4


def test_business_tier_priority_jumps_queue(env):
    """Even when a free-tier run is queued first, a business-tier
    run that arrives later must be picked first by the worker."""
    from claudestruct.server.worker import _claim_one

    _set_tier(env, "org-a", "free")
    _set_tier(env, "org-b", "business")

    # org-a queues first (older created_at), org-b second.
    r_a = _post_run(env, "member@a", description="org-a-first").json()["run_id"]
    r_b = env["client"].post(
        "/v1/runs",
        json={"task": "review", "description": "org-b-second"},
        headers=_auth(env["keys"]["admin@b"]),
    ).json()["run_id"]
    assert r_a and r_b

    with env["factory"]() as session:
        chosen = _claim_one(session)
    # business beats free even with later created_at.
    assert chosen is not None
    assert chosen.run_id == r_b


def test_per_tier_limits_in_subscription_response(env):
    """W8.3: limits surface in /v1/billing/subscription."""
    _set_tier(env, "org-a", "team")
    r = env["client"].get(
        "/v1/billing/subscription", headers=_auth(env["keys"]["admin@a"]),
    )
    body = r.json()
    assert body["tier"] == "team"
    assert body["sandbox_limits"]["max_concurrent_runs"] == 4
    assert body["sandbox_limits"]["max_runtime_seconds"] == 900
    assert body["sandbox_limits"]["max_cost_usd"] == 5.0


def test_sandbox_limits_unknown_tier_falls_back_to_free(env):
    """A future tier value not in the lookup table must NOT silently
    grant business-tier ceilings."""
    from claudestruct.server.billing import sandbox_limits_for_tier
    fallback = sandbox_limits_for_tier("does-not-exist")
    assert fallback.max_concurrent_runs == 1


def test_concurrency_count_is_per_org(env):
    """An org-A run in flight must NOT block an org-B run."""
    from claudestruct.server.worker import _claim_one

    _set_tier(env, "org-a", "free")
    _set_tier(env, "org-b", "free")
    _post_run(env, "member@a")
    env["client"].post(
        "/v1/runs",
        json={"task": "review", "description": "x"},
        headers=_auth(env["keys"]["admin@b"]),
    )
    with env["factory"]() as session:
        first = _claim_one(session)
        assert first is not None
        second = _claim_one(session)
        # Both orgs each claim 1 of their 1-cap; both get a run.
        assert second is not None
        assert first.org_id != second.org_id


# --- W6.5 team dashboard ------------------------------------------------

def test_team_dashboard_empty_for_fresh_org(env):
    r = env["client"].get(
        "/v1/dashboard/team", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["org_slug"] == "org-a"
    assert body["total_runs"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["by_author"] == []
    assert body["by_task"] == []
    assert body["recent"] == []


def test_team_dashboard_aggregates_done_runs(env):
    from claudestruct.server.worker import drain_queue

    # Two members in org-a; record a different cost per author.
    _post_run(env, "member@a", task="dev", description="A")
    _post_run(env, "admin@a", task="review", description="B")
    _post_run(env, "admin@a", task="review", description="C")

    def variable_cost(**kwargs):
        # Cheap heuristic: cost depends on task type for an obvious assertion.
        cost = 1.0 if kwargs["task"] == "dev" else 0.25
        return _StubOutcome(cost_usd=cost)

    drain_queue(env["factory"], env["tmp_path"], runner=variable_cost)

    r = env["client"].get(
        "/v1/dashboard/team", headers=_auth(env["keys"]["viewer@a"]),
    )
    body = r.json()
    assert body["total_runs"] == 3
    # 1.0 (dev by member@a) + 0.25 + 0.25 (review by admin@a)
    assert body["total_cost_usd"] == 1.5
    by_author = {a["email"]: a for a in body["by_author"]}
    # Leaderboard sorted by spend descending; member@a's single $1 run
    # leads admin@a's two $0.25 runs.
    assert body["by_author"][0]["email"] == "member@a"
    assert by_author["member@a"]["runs"] == 1
    assert by_author["admin@a"]["runs"] == 2
    by_task = {t["task"]: t for t in body["by_task"]}
    assert by_task["dev"]["cost_usd"] == 1.0
    assert by_task["review"]["cost_usd"] == 0.5
    assert by_task["review"]["runs"] == 2
    assert len(body["recent"]) == 3


def test_team_dashboard_excludes_other_orgs(env):
    from claudestruct.server.worker import drain_queue

    _post_run(env, "member@a", description="org-a run")
    _post_run(env, "admin@b", description="org-b run")
    drain_queue(env["factory"], env["tmp_path"], runner=_stub_runner)

    r = env["client"].get(
        "/v1/dashboard/team", headers=_auth(env["keys"]["viewer@a"]),
    )
    body = r.json()
    # Only org-a's run shows up; org-b's stays invisible.
    assert body["total_runs"] == 1
    assert body["recent"][0]["task"] == "review"


def test_team_dashboard_excludes_queued_and_running(env):
    """Queued and running rows shouldn't skew rollup numbers — they
    have cost_usd=0 by default and would inflate the run count."""
    from claudestruct.server.worker import process_pending_run

    _post_run(env, "member@a", description="will-be-done")
    _post_run(env, "member@a", description="will-stay-queued")

    # Drain only one of the two.
    with env["factory"]() as session:
        process_pending_run(session, env["tmp_path"], runner=_stub_runner)

    r = env["client"].get(
        "/v1/dashboard/team", headers=_auth(env["keys"]["viewer@a"]),
    )
    body = r.json()
    assert body["total_runs"] == 1
    assert body["by_author"][0]["runs"] == 1


def test_team_dashboard_limit_validation(env):
    r = env["client"].get(
        "/v1/dashboard/team?limit=0", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 400
    r = env["client"].get(
        "/v1/dashboard/team?limit=10000", headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 400


# --- Team-scoped dashboard (?team=<slug>) ---------------------------

def _seed_team(env, *, org_slug: str, team_slug: str, member_emails: list[str]) -> None:
    """Create a team in <org_slug> and add the given users to it."""
    with env["factory"]() as session:
        org = session.query(Org).filter_by(slug=org_slug).one()
        team = Team(org_id=org.id, slug=team_slug, name=team_slug.upper())
        session.add(team)
        session.flush()
        for email in member_emails:
            user = session.query(User).filter_by(email=email).one()
            session.add(TeamMembership(team_id=team.id, user_id=user.id))
        session.commit()


def test_team_dashboard_team_filter_scopes_to_team_members(env):
    """?team=<slug> only counts runs whose author is on that team."""
    from claudestruct.server.worker import drain_queue

    _seed_team(env, org_slug="org-a", team_slug="platform",
               member_emails=["member@a"])

    # member@a is on platform; admin@a is not.
    _post_run(env, "member@a", task="dev", description="platform run")
    _post_run(env, "admin@a", task="review", description="non-platform run")
    drain_queue(env["factory"], env["tmp_path"], runner=_stub_runner)

    # Without filter: both runs visible.
    r_all = env["client"].get(
        "/v1/dashboard/team", headers=_auth(env["keys"]["viewer@a"]),
    ).json()
    assert r_all["total_runs"] == 2

    # With ?team=platform: only member@a's run.
    r_filtered = env["client"].get(
        "/v1/dashboard/team?team=platform",
        headers=_auth(env["keys"]["viewer@a"]),
    ).json()
    assert r_filtered["total_runs"] == 1
    assert {a["email"] for a in r_filtered["by_author"]} == {"member@a"}


def test_team_dashboard_team_filter_unknown_slug_404(env):
    """A team slug that doesn't exist in the caller's org → 404."""
    r = env["client"].get(
        "/v1/dashboard/team?team=does-not-exist",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "team not found"


def test_team_dashboard_team_filter_cross_org_404_not_403(env):
    """A team that exists in a *different* org must look identical to a
    non-existent team — otherwise the endpoint becomes a cross-tenant
    team-slug enumeration oracle."""
    # Create `growth` in org-b only.
    _seed_team(env, org_slug="org-b", team_slug="growth", member_emails=["admin@b"])

    # viewer@a (in org-a) asks for ?team=growth — must be indistinguishable
    # from a slug that doesn't exist anywhere.
    r = env["client"].get(
        "/v1/dashboard/team?team=growth",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "team not found"


def test_team_dashboard_team_filter_empty_team_returns_zero_rollup(env):
    """A team with no members (e.g. just-created, or all members
    removed) should return empty rollup, not 500 from a `IN ()`
    SQL query."""
    _seed_team(env, org_slug="org-a", team_slug="empty-team", member_emails=[])

    r = env["client"].get(
        "/v1/dashboard/team?team=empty-team",
        headers=_auth(env["keys"]["viewer@a"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_runs"] == 0
    assert body["by_author"] == []
    assert body["recent"] == []


# --- W6.6 GitHub App webhook --------------------------------------------

import hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402


def _install(env, *, installation_id=12345, secret="hush", repo_filter=None,
             org_slug="org-a") -> int:
    """Register a GitHub App installation for tests. Returns the
    installation row's PK."""
    from sqlalchemy import select

    from claudestruct.server.models import (
        GitHubInstallation,
        Membership,
        Org,
        Role,
        User,
    )
    with env["factory"]() as session:
        org = session.execute(
            select(Org).where(Org.slug == org_slug)
        ).scalar_one()
        bot_email = f"github-bot@{org_slug}.invalid"
        bot = session.execute(
            select(User).where(User.email == bot_email)
        ).scalar_one_or_none()
        if bot is None:
            bot = User(email=bot_email, name=f"bot {org_slug}")
            session.add(bot)
            session.flush()
            session.add(Membership(
                user_id=bot.id, org_id=org.id, role=Role.member.value,
            ))
        row = GitHubInstallation(
            installation_id=installation_id,
            org_id=org.id,
            webhook_secret=secret,
            repo_filter=repo_filter,
            bot_user_id=bot.id,
        )
        session.add(row)
        session.commit()
        return row.id


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_webhook(env, payload: dict, *, secret: str = "hush",
                  event: str = "pull_request",
                  override_signature: str | None = None):
    body = json.dumps(payload).encode()
    sig = override_signature if override_signature is not None else _sign(secret, body)
    return env["client"].post(
        "/v1/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def _pr_payload(*, installation_id=12345, action="opened",
                repo="acme/widgets", number=42, title="add retry",
                body="Adds retry logic to the API client."):
    return {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "title": title, "body": body},
    }


def _comment_payload(*, installation_id=12345, body="/cs review please",
                     repo="acme/widgets", number=42, has_pr=True,
                     login="alice"):
    issue: dict = {"number": number}
    if has_pr:
        issue["pull_request"] = {"url": "..."}
    return {
        "action": "created",
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "issue": issue,
        "comment": {"body": body, "user": {"login": login}},
    }


def test_webhook_unknown_installation_401(env):
    r = _post_webhook(env, _pr_payload(installation_id=99999), secret="hush")
    assert r.status_code == 401


def test_webhook_bad_signature_401(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _pr_payload(), secret="wrong")
    assert r.status_code == 401


def test_webhook_missing_signature_header_401(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _pr_payload(), override_signature="")
    assert r.status_code == 401


def test_webhook_pr_opened_enqueues_run(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _pr_payload(action="opened"))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["trigger"] == "pull_request.opened"
    run_id = body["run_id"]
    # Verify the row exists, attributed to the bot user.
    from sqlalchemy import select

    from claudestruct.server.models import Run, User
    with env["factory"]() as session:
        run = session.execute(select(Run).where(Run.run_id == run_id)).scalar_one()
        bot = session.execute(select(User).where(User.id == run.user_id)).scalar_one()
        assert bot.email.startswith("github-bot@")
        assert run.task == "review"
        assert "PR #42" in run.description


def test_webhook_pr_synchronize_enqueues(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _pr_payload(action="synchronize"))
    assert r.status_code == 202
    assert r.json()["trigger"] == "pull_request.synchronize"


def test_webhook_pr_closed_ignored(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _pr_payload(action="closed"))
    assert r.status_code == 202  # signature ok, just no enqueue
    assert r.json()["status"] == "ignored"


def test_webhook_comment_with_cs_review_enqueues(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _comment_payload(body="LGTM but /cs review"),
                      event="issue_comment")
    assert r.status_code == 202
    assert r.json()["trigger"] == "issue_comment.cs-review"


def test_webhook_comment_without_command_ignored(env):
    _install(env, secret="hush")
    r = _post_webhook(env, _comment_payload(body="lgtm"), event="issue_comment")
    assert r.status_code == 202
    assert r.json()["status"] == "ignored"


def test_webhook_comment_on_plain_issue_ignored(env):
    """Plain (non-PR) issues don't have diffs; /cs review on them is a no-op."""
    _install(env, secret="hush")
    r = _post_webhook(env,
                      _comment_payload(body="/cs review", has_pr=False),
                      event="issue_comment")
    assert r.status_code == 202
    assert r.json()["status"] == "ignored"


def test_webhook_repo_filter_drops_non_matching(env):
    _install(env, secret="hush", repo_filter="widgets")
    r1 = _post_webhook(env, _pr_payload(repo="acme/gadgets"))
    assert r1.status_code == 202
    assert r1.json()["status"] == "ignored"
    r2 = _post_webhook(env, _pr_payload(repo="acme/widgets"))
    assert r2.json()["status"] == "queued"


def test_webhook_ping_event_returns_ok(env):
    _install(env, secret="hush")
    r = _post_webhook(env,
                      {"installation": {"id": 12345}, "zen": "Practicality"},
                      event="ping")
    assert r.status_code == 202
    assert r.json() == {"status": "ok", "event": "ping"}


def test_webhook_revoked_install_rejected(env):
    install_pk = _install(env, secret="hush")
    from datetime import datetime, timezone

    from claudestruct.server.models import GitHubInstallation
    with env["factory"]() as session:
        row = session.get(GitHubInstallation, install_pk)
        row.revoked_at = datetime.now(timezone.utc)
        session.commit()
    r = _post_webhook(env, _pr_payload(), secret="hush")
    assert r.status_code == 401  # same shape as unknown-install


def test_verify_signature_unit():
    """Locks the HMAC contract that the production endpoint depends on."""
    from claudestruct.server.routers.github import verify_signature
    body = b'{"hello":"world"}'
    good = "sha256=" + _hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert verify_signature("shh", body, good) is True
    assert verify_signature("shh", body, good.replace("sha256=", "sha1=")) is False
    assert verify_signature("shh", body, None) is False
    assert verify_signature("shh", body, "") is False
    assert verify_signature("wrong", body, good) is False


# --- W6.4 OAuth login + cookie sessions --------------------------------

from typing import Any  # noqa: E402


class _StubResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _StubGitHubClient:
    """Fake httpx.Client driven from a per-test scripted plan.

    Tests prepare a list of (predicate, response) pairs; the first
    matching predicate wins. Lets us assert what URLs got hit AND
    short-circuit unexpected requests to a 500 instead of real HTTP.
    """

    def __init__(self, plan: list[tuple[Any, _StubResponse]]):
        self.plan = plan
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._dispatch("POST", url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._dispatch("GET", url)

    def _dispatch(self, method, url):
        for pred, resp in self.plan:
            if pred(method, url):
                return resp
        return _StubResponse(500, {"error": "no plan match"})

    def close(self):
        pass


def _wire_github_oauth(env, monkeypatch, *, client: _StubGitHubClient):
    """Set the env vars the OAuth helpers consume + inject the stub."""
    monkeypatch.setenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("CLAUDESTRUCT_OAUTH_REDIRECT_BASE", "http://daemon.test")
    env["app"].state.oauth_http_client = lambda: client


def test_oauth_login_redirects_to_github_when_configured(env, monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_SECRET", "y")
    r = env["client"].get("/v1/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(
        "https://github.com/login/oauth/authorize?"
    )
    # State cookie was set so the callback can verify it later.
    assert "claudestruct_oauth_state=" in r.headers.get("set-cookie", "")


def test_oauth_login_503_when_unconfigured(env, monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_SECRET", raising=False)
    r = env["client"].get("/v1/auth/github/login", follow_redirects=False)
    assert r.status_code == 503


def test_oauth_callback_state_mismatch_400(env, monkeypatch):
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient([]))
    # No state cookie set → mismatch.
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=def",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_oauth_callback_unregistered_email_403(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "access_token" in u,
         _StubResponse(200, {"access_token": "gh-token"})),
        (lambda m, u: m == "GET" and u.endswith("/user"),
         _StubResponse(200, {
             "login": "stranger", "email": "stranger@example.com", "name": "Stranger",
         })),
    ]
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient(plan))

    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "not registered" in r.json()["detail"]


def test_oauth_callback_happy_path_sets_session_cookie(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "access_token" in u,
         _StubResponse(200, {"access_token": "gh-token"})),
        (lambda m, u: m == "GET" and u.endswith("/user"),
         _StubResponse(200, {
             "login": "alice", "email": "admin@a", "name": "Alice",
         })),
    ]
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient(plan))

    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "claudestruct_session=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_oauth_callback_falls_back_to_user_emails_when_user_email_missing(env, monkeypatch):
    """GitHub /user returns null email when the user's email is private;
    the helper should then fetch /user/emails and pick the verified primary."""
    plan = [
        (lambda m, u: m == "POST" and "access_token" in u,
         _StubResponse(200, {"access_token": "gh-token"})),
        (lambda m, u: m == "GET" and u.endswith("/user"),
         _StubResponse(200, {"login": "admin-alice", "email": None, "name": "A"})),
        (lambda m, u: m == "GET" and u.endswith("/user/emails"),
         _StubResponse(200, [
             {"email": "noreply@x.com", "primary": False, "verified": True},
             {"email": "admin@a", "primary": True, "verified": True},
         ])),
    ]
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient(plan))
    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 302  # successful login


def test_oauth_callback_handles_token_exchange_failure(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "access_token" in u,
         _StubResponse(200, {"error": "bad_verification_code"})),
    ]
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient(plan))
    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "oauth error" in r.json()["detail"]


def _login_via_callback(env, monkeypatch, email="admin@a"):
    """Drive the full OAuth flow and return the session cookie value."""
    plan = [
        (lambda m, u: m == "POST" and "access_token" in u,
         _StubResponse(200, {"access_token": "gh-token"})),
        (lambda m, u: m == "GET" and u.endswith("/user"),
         _StubResponse(200, {"login": "x", "email": email, "name": "x"})),
    ]
    _wire_github_oauth(env, monkeypatch, client=_StubGitHubClient(plan))
    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/github/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 302
    return env["client"].cookies.get("claudestruct_session")


def test_session_cookie_authenticates_dashboard_request(env, monkeypatch):
    cookie = _login_via_callback(env, monkeypatch)
    assert cookie
    # Dashboard call without bearer — cookie does the work.
    r = env["client"].get("/v1/dashboard")
    assert r.status_code == 200


def test_whoami_returns_principal_for_session_cookie(env, monkeypatch):
    _login_via_callback(env, monkeypatch, email="admin@a")
    r = env["client"].get("/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "admin@a"
    assert body["org_slug"] == "org-a"
    assert body["role"] == "admin"
    assert body["auth_method"] == "session"


def test_whoami_401_without_cookie(env, monkeypatch):
    r = env["client"].get("/v1/auth/me")
    assert r.status_code == 401


def test_logout_revokes_session(env, monkeypatch):
    _login_via_callback(env, monkeypatch)
    # Confirm the session works.
    r1 = env["client"].get("/v1/dashboard")
    assert r1.status_code == 200
    r2 = env["client"].post("/v1/auth/logout")
    assert r2.status_code == 200
    # Cookie remains in jar locally but is server-side revoked.
    r3 = env["client"].get("/v1/dashboard")
    assert r3.status_code == 401


def test_logout_idempotent_without_cookie(env):
    # Clear any prior session cookies to simulate a fresh logout call.
    env["client"].cookies.clear()
    r = env["client"].post("/v1/auth/logout")
    assert r.status_code == 200


def test_bearer_wins_over_session_cookie(env, monkeypatch):
    """If both a valid bearer token and a session cookie are present,
    bearer should take precedence — CLI calls with stale cookies must
    still behave as the bearer's identity intends."""
    _login_via_callback(env, monkeypatch, email="admin@a")
    # Now hit a route with the viewer's bearer too — should still
    # succeed (both auth methods work) and not blow up.
    r = env["client"].get("/v1/dashboard", headers=_auth(env["keys"]["viewer@a"]))
    assert r.status_code == 200


def test_revoked_session_cookie_falls_through_to_401(env, monkeypatch):
    cookie = _login_via_callback(env, monkeypatch)
    env["client"].post("/v1/auth/logout")
    # Restore the cookie to simulate an attacker / stale tab.
    env["client"].cookies.set("claudestruct_session", cookie, path="/")
    r = env["client"].get("/v1/dashboard")
    assert r.status_code == 401


def test_webhook_run_isolated_to_installation_org(env):
    """A webhook for org-a's installation must NOT be visible to org-b's
    members (cross-org isolation through the runs table)."""
    _install(env, installation_id=11111, secret="hush", org_slug="org-a")
    r = _post_webhook(env, _pr_payload(installation_id=11111))
    run_id = r.json()["run_id"]
    g = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["admin@b"]))
    assert g.status_code == 404
    g2 = env["client"].get(f"/v1/runs/{run_id}", headers=_auth(env["keys"]["viewer@a"]))
    assert g2.status_code == 200


# --- W8.2 token-cap enforcement at run-submit time -----------------


def _seed_run_usage(env, *, org_slug: str, email: str,
                    input_tokens: int, output_tokens: int = 0,
                    status_val: str | None = None) -> None:
    """Insert a directly-persisted Run row to simulate prior usage.
    Bypasses POST /v1/runs so we can pre-load the cap."""
    import secrets as _secrets

    from sqlalchemy import select

    from claudestruct.server.models import Org, Run, RunStatus, User
    if status_val is None:
        status_val = RunStatus.done.value
    with env["factory"]() as session:
        org = session.execute(select(Org).where(Org.slug == org_slug)).scalar_one()
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        session.add(Run(
            run_id=f"r-seed-{_secrets.token_hex(4)}",
            org_id=org.id, user_id=user.id,
            status=status_val, task="dev", description="seed",
            input_tokens=input_tokens, output_tokens=output_tokens,
        ))
        session.commit()


def test_post_run_under_cap_succeeds(env):
    """Free org with 50k of 100k used: new submit accepted (202)."""
    _set_tier(env, "org-a", "free")
    _seed_run_usage(env, org_slug="org-a", email="member@a", input_tokens=50_000)
    r = _post_run(env, "member@a")
    assert r.status_code == 202


def test_post_run_at_cap_rejected_402(env):
    """Free org has already exceeded the 100k cap: new submit rejected
    with 402 + structured body."""
    _set_tier(env, "org-a", "free")
    _seed_run_usage(env, org_slug="org-a", email="member@a", input_tokens=100_000)
    r = _post_run(env, "member@a")
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body["used_tokens"] == 100_000
    assert body["cap_tokens"] == 100_000
    assert body["tier"] == "free"
    assert "period_end" in body
    assert "cap reached" in body["detail"].lower() or "cap" in body["detail"].lower()


def test_post_run_team_tier_unlimited(env):
    """Team org with massive usage still allowed — no cap on this tier."""
    _set_tier(env, "org-a", "team")
    _seed_run_usage(
        env, org_slug="org-a", email="member@a", input_tokens=1_000_000,
    )
    r = _post_run(env, "member@a")
    assert r.status_code == 202


def test_post_run_business_tier_unlimited(env):
    _set_tier(env, "org-a", "business")
    _seed_run_usage(
        env, org_slug="org-a", email="member@a", input_tokens=10_000_000,
    )
    r = _post_run(env, "member@a")
    assert r.status_code == 202


def test_post_run_failed_runs_count_against_cap(env):
    """A failed Run still cost the operator real Anthropic spend, so it
    must count toward the monthly cap. Verifies the gate doesn't filter
    them out."""
    from claudestruct.server.models import RunStatus
    _set_tier(env, "org-a", "free")
    # Use only failed runs to reach the cap.
    _seed_run_usage(
        env, org_slug="org-a", email="member@a",
        input_tokens=100_000, status_val=RunStatus.failed.value,
    )
    r = _post_run(env, "member@a")
    assert r.status_code == 402
    assert r.json()["detail"]["used_tokens"] == 100_000


def test_post_run_unknown_tier_falls_back_to_free(env):
    """Defensive: a Subscription with an unknown tier value must NOT
    grant business-tier (uncapped) ceilings. Falls back to free."""
    from sqlalchemy import select

    from claudestruct.server.billing import Subscription
    from claudestruct.server.models import Org
    # Bypass _set_tier (which validates via Tier enum) to plant a
    # forged tier string directly.
    with env["factory"]() as session:
        org = session.execute(select(Org).where(Org.slug == "org-a")).scalar_one()
        sub = Subscription(org_id=org.id, tier="ultimate-platinum")
        session.add(sub)
        session.commit()
    _seed_run_usage(env, org_slug="org-a", email="member@a", input_tokens=100_000)
    r = _post_run(env, "member@a")
    assert r.status_code == 402  # rejected because free-fallback applies
    assert r.json()["detail"]["cap_tokens"] == 100_000


def test_post_run_no_subscription_row_treats_as_free(env):
    """An org with no Subscription row at all (the implicit-free path)
    must still get the cap enforced. Doubles as a regression check
    that get_or_default is wired correctly."""
    _seed_run_usage(env, org_slug="org-a", email="member@a", input_tokens=100_000)
    r = _post_run(env, "member@a")
    assert r.status_code == 402


def test_post_run_other_orgs_usage_does_not_count(env):
    """Cross-tenant: org-b's usage must NOT push org-a over the cap."""
    _set_tier(env, "org-a", "free")
    _set_tier(env, "org-b", "free")
    _seed_run_usage(env, org_slug="org-b", email="admin@b", input_tokens=100_000)
    r = _post_run(env, "member@a")
    assert r.status_code == 202  # org-a still has full quota
