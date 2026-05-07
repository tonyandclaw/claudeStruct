"""Request-latency tracking for the FastAPI server (W8.7).

Thread-safe in-memory aggregator for per-route HTTP request timings.
Exposed as Prometheus text format via ``GET /v1/slo/latency``.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Histogram buckets for API response times (seconds). Aligned with
# common Prometheus histogram buckets but tuned for a CLI/API server
# where p95 under 500ms is the SLO target.
LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


def _escape(s: str) -> str:
    """Escape label values for Prometheus exposition format."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class _RouteLatency:
    """Aggregated latencies for a single route path."""
    counts: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    total: float = 0.0
    n: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, latency_seconds: float) -> None:
        with self.lock:
            self.total += latency_seconds
            self.n += 1
            for bucket in LATENCY_BUCKETS:
                if latency_seconds <= bucket:
                    self.counts[bucket] += 1

    def percentile(self, p: float) -> float | None:
        if self.n == 0:
            return None
        sorted_buckets = sorted(LATENCY_BUCKETS)
        cum = 0
        for b in sorted_buckets:
            cum += self.counts[b]
            if cum / self.n >= p / 100.0:
                return b
        return sorted_buckets[-1]


class LatencyTracker:
    """Thread-safe in-memory latency aggregator.

    Tracks per-route p50/p95/p99 without an external metrics library.
    Exposed via ``GET /v1/slo/latency`` as Prometheus text format.
    """

    def __init__(self) -> None:
        self._routes: dict[str, _RouteLatency] = defaultdict(_RouteLatency)
        self._lock = threading.Lock()

    def record(self, route: str, latency_seconds: float) -> None:
        self._routes[route].record(latency_seconds)

    def render_prometheus(self) -> str:
        """Render all tracked routes as Prometheus text exposition."""
        lines: list[str] = []
        for route, lat in sorted(self._routes.items()):
            with lat.lock:
                n = lat.n
                total = lat.total
                p50 = lat.percentile(50)
                p95 = lat.percentile(95)
                p99 = lat.percentile(99)
            r = _escape(route)
            q50 = p50 if p50 is not None else 0
            q95 = p95 if p95 is not None else 0
            q99 = p99 if p99 is not None else 0
            lines.append(
                "# HELP claudestruct_http_request_latency_seconds "
                "HTTP request latency by route"
            )
            lines.append(
                "# TYPE claudestruct_http_request_latency_seconds summary"
            )
            lines.append(
                f'claudestruct_http_request_latency_seconds'
                f'{{route="{r}",n={n},quantile="0.5"}} {q50}'
            )
            lines.append(
                f'claudestruct_http_request_latency_seconds'
                f'{{route="{r}",n={n},quantile="0.95"}} {q95}'
            )
            lines.append(
                f'claudestruct_http_request_latency_seconds'
                f'{{route="{r}",n={n},quantile="0.99"}} {q99}'
            )
            lines.append(
                "# HELP claudestruct_http_request_total "
                "Total HTTP requests by route"
            )
            lines.append(
                "# TYPE claudestruct_http_request_total counter"
            )
            lines.append(
                f'claudestruct_http_request_total{{route="{r}"}} {n}'
            )
            lines.append(
                "# HELP claudestruct_http_request_latency_sum "
                "Total HTTP request latency sum by route"
            )
            lines.append(
                "# TYPE claudestruct_http_request_latency_sum counter"
            )
            lines.append(
                f'claudestruct_http_request_latency_sum{{route="{r}"}} {total}'
            )
        return "\n".join(lines)


class RequestLatencyMiddleware(BaseHTTPMiddleware):
    """Time every HTTP request and record p50/p95/p99 per route.

    Route paths are normalized by replacing dynamic segments (e.g.
    ``/v1/runs/abc123`` → ``/v1/runs/{id}``) so cardinality stays
    bounded. Uses Starlette's ``request.scope["route"]`` when available
    for the static template; falls back to ``request.url.path``.
    """

    def __init__(self, app: Any, tracker: LatencyTracker) -> None:
        super().__init__(app)
        self._tracker = tracker

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        # Normalize route to static template where available.
        # request.scope["route"] is a starlette Route object; read its
        # path attribute for the static template instead of using the
        # Route object itself (unhashable in FastAPI).
        route_obj = request.scope.get("route")
        route = getattr(route_obj, "path", None) or request.url.path or "/"
        self._tracker.record(route, elapsed)
        return response
