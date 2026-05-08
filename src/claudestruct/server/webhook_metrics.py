"""Webhook + side-effect error counter (C.3).

Several worker / router paths run "best-effort" outbound IO (GitHub
comment posts, Stripe subscription fetches, etc.) and silently
swallow exceptions to keep the producer's terminal status from
rolling back. The swallow path logs an exception line, but a flood
of silent failures is invisible to operators unless they tail logs.

This module is the visibility layer: each swallow site bumps a
labelled counter, which the existing ``/v1/slo/metrics`` endpoint
renders alongside the latency histogram.

API:

    from claudestruct.server.webhook_metrics import WEBHOOK_ERRORS

    try:
        post_to_github(...)
    except GitHubAppError as exc:
        WEBHOOK_ERRORS.bump(path="github.verdict_comment", reason="github_app_error")
        log.warning(...)

Test seam: ``reset()`` zeros every counter so each test starts
clean. The singleton is module-level by design — threading a
``Counter`` through every helper signature would be invasive and
the alternative (FastAPI ``app.state``) doesn't reach into the
worker daemon.
"""
from __future__ import annotations

import threading
from collections import defaultdict


def _escape(s: str) -> str:
    """Escape label values for Prometheus exposition format."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class WebhookErrorCounter:
    """Thread-safe ``{(path, reason) -> count}`` counter.

    Two-key shape so the Prometheus output stays machine-greppable:
    ``path`` is which silenced helper bumped it (e.g.
    ``github.verdict_comment``), ``reason`` is the exception family
    (``github_app_error`` / ``unexpected`` / ``stripe_error`` /
    ``upstream_5xx`` / etc.). Operators can alert on
    ``rate(claudestruct_webhook_errors_total{path="github.pr_open"}[5m])``
    without grep-parsing log lines.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def bump(self, *, path: str, reason: str, by: int = 1) -> None:
        if by <= 0:
            return
        with self._lock:
            self._counts[(path, reason)] += by

    def get(self, *, path: str, reason: str) -> int:
        with self._lock:
            return self._counts.get((path, reason), 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def reset(self) -> None:
        """Test seam — zero every counter."""
        with self._lock:
            self._counts.clear()

    def render_prometheus(self) -> str:
        """Render the full table as Prometheus text exposition."""
        with self._lock:
            snapshot = dict(self._counts)
        lines: list[str] = [
            "# HELP claudestruct_webhook_errors_total "
            "Best-effort outbound IO failures (silently swallowed)",
            "# TYPE claudestruct_webhook_errors_total counter",
        ]
        for (path, reason), count in sorted(snapshot.items()):
            lines.append(
                f'claudestruct_webhook_errors_total'
                f'{{path="{_escape(path)}",reason="{_escape(reason)}"}} {count}'
            )
        return "\n".join(lines) + "\n"


# Module-level singleton. See module docstring for why.
WEBHOOK_ERRORS = WebhookErrorCounter()
