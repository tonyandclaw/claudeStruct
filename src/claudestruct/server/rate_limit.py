"""In-process rate limiting (production-readiness).

Token-bucket per (org_id | client_ip), enforced as middleware on
the FastAPI app. Off by default — operators that want a
per-tenant ceiling set ``CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE`` to a
positive integer.

Why in-process AND not slowapi:

- Pure stdlib — no extra dep on the lean install path
- Token bucket is small enough to read inline (~80 LOC)
- Operators who want cross-replica fairness already run the LB
  rate limiter described in the threat model. This middleware
  is **defence in depth**, not the primary control.

Why bucket per (org_id | ip), not just ip:

- Authenticated requests carry org context; rate-limiting by IP
  would let one IP behind a CGNAT exhaust the bucket for many
  customers
- Unauthenticated requests (`/healthz`, `/status`, OAuth
  callback) fall back to IP

The middleware is mounted by ``server/app.py`` only when the env
var is set. When unset, the middleware isn't installed at all —
zero overhead.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class _Bucket:
    """Single-key token bucket.

    Capacity = burst size. Refill rate = capacity / window_seconds
    so a steady stream at the target rate never depletes.
    """

    __slots__ = ("capacity", "refill_per_s", "tokens", "last_refill")

    def __init__(self, capacity: int, refill_per_s: float,
                 *, now: float | None = None) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.tokens = float(capacity)
        # Pin `last_refill` to whatever clock the first consume()
        # call will use. For prod we use monotonic; tests pass
        # `now=0.0` and call consume(now=0.0) for a deterministic
        # baseline.
        self.last_refill = now if now is not None else time.monotonic()

    def consume(self, now: float) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds).

        On allow, retry_after is 0. On deny, retry_after is the
        seconds until ONE token is available (so the caller can
        emit it as the `Retry-After` header).
        """
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(
            self.capacity, self.tokens + elapsed * self.refill_per_s,
        )
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        # Need to wait for (1 - tokens) more tokens at refill_per_s.
        deficit = 1.0 - self.tokens
        retry_after = deficit / self.refill_per_s if self.refill_per_s else 60.0
        return False, retry_after


class TokenBucketRateLimiter:
    """Thread-safe rate limiter keyed by request identity.

    ``per_minute`` is the steady-state ceiling; the bucket capacity
    is set to the same number so a burst up to ``per_minute``
    requests in a single second is allowed before the bucket
    empties.

    Resolution helper ``key_for(request)`` extracts the principal
    (when set on ``request.state``) or falls back to the client
    IP. Tests can pass a custom ``key_fn`` to drive specific
    scenarios.
    """

    def __init__(
        self,
        *,
        per_minute: int,
        key_fn: Callable[[Request], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.per_minute = per_minute
        self._refill_per_s = per_minute / 60.0
        self._key_fn = key_fn or self._default_key
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_key(request: Request) -> str:
        principal = getattr(request.state, "principal", None)
        if principal is not None and getattr(principal, "org_id", None) is not None:
            return f"org:{principal.org_id}"
        client = request.client
        ip = client.host if client else "unknown"
        return f"ip:{ip}"

    def check(self, request: Request) -> tuple[bool, float]:
        key = self._key_fn(request)
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    self.per_minute, self._refill_per_s, now=now,
                )
                self._buckets[key] = bucket
            return bucket.consume(now)

    def reset(self) -> None:
        """Test seam — clear every bucket."""
        with self._lock:
            self._buckets.clear()


# --- Middleware ----------------------------------------------------


# Routes that should never be rate-limited — health checks and
# status pages are scraped by monitoring infra and a 429 there
# would mask real outages by triggering noisy alerts. The /metrics
# endpoints are similarly poll-driven and shouldn't burn the
# bucket.
EXEMPT_PATHS = frozenset({
    "/healthz",
    "/readyz",
    "/status",
    "/v1/slo",
    "/v1/slo/latency",
    "/v1/slo/webhook_errors",
})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply ``limiter.check`` to every non-exempt request. On
    deny, return 429 with `Retry-After`."""

    def __init__(self, app: Any, limiter: TokenBucketRateLimiter) -> None:
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):  # noqa: D401
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        allowed, retry_after = self.limiter.check(request)
        if not allowed:
            # Round up so a sub-second retry doesn't read as "0".
            seconds = max(1, int(retry_after + 0.999))
            return JSONResponse(
                {
                    "detail": "rate limit exceeded",
                    "retry_after_s": seconds,
                },
                status_code=429,
                headers={"Retry-After": str(seconds)},
            )
        return await call_next(request)


def resolve_per_minute(override: int | None = None) -> int | None:
    """Resolve the configured per-minute ceiling.

    Priority: explicit override > ``CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE``
    env > ``None`` (off).

    Returns ``None`` when the limiter should NOT be installed —
    operators relying on the LB for rate control don't pay the
    middleware cost.
    """
    if override is not None:
        return override if override > 0 else None
    raw = os.environ.get("CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"CLAUDESTRUCT_RATE_LIMIT_PER_MINUTE={raw!r} is not an integer",
        ) from exc
    return n if n > 0 else None
