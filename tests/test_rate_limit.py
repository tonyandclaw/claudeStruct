"""Tests for the in-process rate limiter (production-readiness).

Verifies the token-bucket math, the env-driven on/off switch, the
exempt-path list (health checks must never 429), and end-to-end
middleware integration via the FastAPI test client.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import create_app
from claudestruct.server.db import init_db
from claudestruct.server.rate_limit import (
    EXEMPT_PATHS,
    TokenBucketRateLimiter,
    _Bucket,
    resolve_per_minute,
)

# --- _Bucket math ---------------------------------------------------


def test_bucket_starts_full():
    b = _Bucket(capacity=10, refill_per_s=1.0, now=0.0)
    allowed, _ = b.consume(now=0.0)
    assert allowed is True


def test_bucket_depletes_then_denies():
    b = _Bucket(capacity=3, refill_per_s=0.0, now=0.0)  # no refill
    assert b.consume(now=0.0)[0] is True
    assert b.consume(now=0.0)[0] is True
    assert b.consume(now=0.0)[0] is True
    allowed, retry = b.consume(now=0.0)
    assert allowed is False
    # No refill rate → fallback retry-after of 60s.
    assert retry > 0


def test_bucket_refills_over_time():
    b = _Bucket(capacity=2, refill_per_s=1.0, now=0.0)
    b.consume(now=0.0)
    b.consume(now=0.0)
    # Empty now.
    assert b.consume(now=0.0)[0] is False
    # 1.0s later we have one token back.
    assert b.consume(now=1.0)[0] is True


def test_bucket_caps_refill_at_capacity():
    """Idle bucket doesn't accumulate beyond capacity."""
    b = _Bucket(capacity=5, refill_per_s=1.0, now=0.0)
    b.consume(now=0.0)  # 4 left
    # Wait an hour; should still cap at 5, not 3604.
    for _ in range(5):
        assert b.consume(now=3600.0)[0] is True
    assert b.consume(now=3600.0)[0] is False


def test_bucket_retry_after_reflects_deficit():
    b = _Bucket(capacity=1, refill_per_s=2.0, now=0.0)  # 1 token / 0.5s
    b.consume(now=0.0)  # spent it
    allowed, retry = b.consume(now=0.0)
    assert allowed is False
    # Need 1 more token at 2/s → 0.5s wait.
    assert retry == pytest.approx(0.5)


# --- TokenBucketRateLimiter ----------------------------------------


def test_limiter_keys_by_principal_org_when_set():
    """Authenticated requests bucket per org; not per IP. A team
    sharing one office IP shouldn't share a bucket with other
    customers."""
    from starlette.requests import Request

    class _FakePrincipal:
        org_id = 42

    def make_req(org: int) -> Request:
        scope = {
            "type": "http", "method": "GET", "path": "/", "client": ("1.1.1.1", 0),
            "headers": [], "raw_path": b"/", "query_string": b"",
            "scheme": "http", "server": ("test", 80), "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "state": {"principal": type("P", (), {"org_id": org})()},
        }
        r = Request(scope)
        # Starlette doesn't auto-fill state from scope; do it
        # manually so the limiter sees the principal.
        r.state.principal = scope["state"]["principal"]
        return r

    lim = TokenBucketRateLimiter(per_minute=2)
    # org 1 uses both tokens; org 2 still has full bucket.
    assert lim.check(make_req(1))[0]
    assert lim.check(make_req(1))[0]
    assert lim.check(make_req(1))[0] is False
    assert lim.check(make_req(2))[0] is True


def test_limiter_falls_back_to_ip_for_unauthenticated():
    """Unauthenticated requests bucket per source IP."""
    from starlette.requests import Request

    def make_req(ip: str) -> Request:
        scope = {
            "type": "http", "method": "GET", "path": "/", "client": (ip, 0),
            "headers": [], "raw_path": b"/", "query_string": b"",
            "scheme": "http", "server": ("test", 80), "asgi": {"version": "3.0"},
            "http_version": "1.1",
        }
        return Request(scope)

    lim = TokenBucketRateLimiter(per_minute=1)
    assert lim.check(make_req("1.1.1.1"))[0]
    assert lim.check(make_req("1.1.1.1"))[0] is False
    assert lim.check(make_req("2.2.2.2"))[0] is True


def test_limiter_rejects_non_positive_per_minute():
    with pytest.raises(ValueError, match="per_minute"):
        TokenBucketRateLimiter(per_minute=0)
    with pytest.raises(ValueError, match="per_minute"):
        TokenBucketRateLimiter(per_minute=-1)


# --- resolve_per_minute --------------------------------------------


def test_resolve_per_minute_default_is_none(monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", raising=False)
    assert resolve_per_minute() is None


def test_resolve_per_minute_env_parses_int(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "60")
    assert resolve_per_minute() == 60


def test_resolve_per_minute_zero_or_negative_treated_as_off(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "0")
    assert resolve_per_minute() is None
    monkeypatch.setenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "-5")
    assert resolve_per_minute() is None


def test_resolve_per_minute_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "60")
    assert resolve_per_minute(120) == 120


def test_resolve_per_minute_invalid_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "not-a-number")
    with pytest.raises(RuntimeError, match="not an integer"):
        resolve_per_minute()


# --- Middleware integration via TestClient -------------------------


def _make_client(tmp_path, *, per_minute):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    app = create_app(
        engine=engine, run_root=str(tmp_path), skip_init=True,
        rate_limit_per_minute=per_minute,
    )
    return TestClient(app)


def test_middleware_off_when_per_minute_unset(tmp_path):
    """No env var → no middleware → unbounded requests."""
    client = _make_client(tmp_path, per_minute=None)
    for _ in range(20):
        r = client.get("/v1/dashboard")
        # Auth required, but never 429.
        assert r.status_code != 429


def test_middleware_returns_429_after_quota(tmp_path):
    client = _make_client(tmp_path, per_minute=2)
    # Per-IP bucket: TestClient shows up as one client.
    r1 = client.get("/v1/dashboard")
    r2 = client.get("/v1/dashboard")
    r3 = client.get("/v1/dashboard")
    # The 401 vs 429 ordering: middleware runs BEFORE auth, so the
    # third request should 429 before reaching the auth dependency.
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 429
    assert r3.headers.get("Retry-After") is not None
    body = r3.json()
    assert body["detail"] == "rate limit exceeded"
    assert body["retry_after_s"] >= 1


def test_middleware_exempts_health_checks(tmp_path):
    """`/healthz` / `/readyz` / `/status` MUST never 429 — a
    monitoring scraper that gets rate-limited would mask real
    outages."""
    client = _make_client(tmp_path, per_minute=1)
    # Burn the quota with a regular request.
    client.get("/v1/dashboard")
    client.get("/v1/dashboard")
    # Now health check — must succeed even though the IP's bucket
    # is empty.
    for path in ("/healthz", "/readyz", "/status"):
        r = client.get(path)
        assert r.status_code != 429, (
            f"{path} returned 429 — health checks must be exempt"
        )


def test_exempt_paths_includes_metrics_endpoints():
    """Lock the exemption list so a refactor can't accidentally
    turn /healthz into a rate-limited route."""
    for required in ("/healthz", "/readyz", "/status",
                     "/v1/slo", "/v1/slo/latency",
                     "/v1/slo/webhook_errors"):
        assert required in EXEMPT_PATHS
