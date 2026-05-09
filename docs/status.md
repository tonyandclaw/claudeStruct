# Public status endpoint

`GET /status` returns a single JSON document a status page can
render. Unauthenticated by design: the payload is exclusively
fleet-wide aggregates plus static deployment facts (version,
region). No tenant data leaks — no org slugs, no run ids, no user
emails.

A status-page widget can poll this every minute without burning a
bearer token.

## Response shape

```json
{
  "version": "0.1.0",
  "region": "us-east-1",
  "overall": "ok",
  "components": [
    {"name": "db",     "state": "ok"},
    {"name": "worker", "state": "ok",       "detail": "queue_depth=3"},
    {"name": "api",    "state": "degraded", "detail": "recent error rate 0.80%"}
  ],
  "queue_depth": 3,
  "last_successful_run_at": "2026-05-08T01:42:00+00:00",
  "recent_error_rate_1h": 0.008,
  "generated_at": "2026-05-08T01:43:11+00:00"
}
```

`overall` is the worst of the per-component states (`down`
beats `degraded` beats `ok`).

## Components

| name    | what it checks                                                          | thresholds                                                            |
| ------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `db`    | round-trips a `SELECT 1` against the configured DATABASE_URL            | `ok` if it succeeds; `down` on any DB error                           |
| `worker`| count of `RunStatus.queued` rows                                        | `ok` < 50 < `degraded` < 500 ≤ `down`                                 |
| `api`   | fleet-wide error rate over the last hour (failed / done+failed)         | `ok` < 0.5% < `degraded` < 5% ≤ `down`                                |

The worker thresholds match the runbook
([`runbook.md`](runbook.md)) so an alert that fires on the worker
component matches what the on-call playbook tells the operator
to do.

## Why not authenticated?

The status page is the exact surface a customer hits BEFORE they
have a working session — "is it me or is it down?" If we required
auth, anyone whose login is broken couldn't tell the difference
between "the site is down" and "my account is broken."

The trade-off: the payload must never carry tenant-specific data.
That's a code-review invariant, not just a design intent — the
test `test_status_endpoint_no_tenant_data_leaks` asserts that no
org slug, user email, or run id appears in the response body.

## Anti-flap rules

- **Queue depth** is the live count, not a smoothed rolling
  average. A momentary spike caused by the worker bouncing will
  flap the component briefly. The status-page client should
  hold a green-streak counter and only flip to red after N
  consecutive failed reads.

- **Recent error rate** is `null` when the fleet has fewer than
  10 terminal runs in the last hour — at low traffic a single
  failure dominates the rate. The component reports `ok` in
  that case rather than oscillating between green and red over
  one fail.

## Next

W8.7 still tracks an external `status.claudestruct.dev` — a
publicly-hosted status page powered by this endpoint. The endpoint
is the API; the SPA is the deferred work.
