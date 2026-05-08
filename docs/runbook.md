# Operator runbook

The on-call playbook for the hosted claudeStruct daemon. Each
section starts with **how to recognise** the symptom (alert text,
metric name, log shape) and ends with **what to do**, in the order
to do it. Every action either resolves the issue or escalates.

If you're new to the system: skim [`server.md`](server.md) and
[`slo.md`](slo.md) first. The deploy layout is in
[`deploy/`](../deploy/).

---

## 1. API latency spike

**Recognise:** Prometheus alert
`claudestruct_http_request_latency_seconds{quantile="0.95"} > 2`
fires for a route. `/v1/slo/latency` shows the offending route.

**Triage:**

1. **DB pressure.** SSH into the daemon host and run
   `cs serve worker --once` — does it complete in seconds or hang?
   - Hangs / errors out → DB is the bottleneck. Check disk on the
     SQLite host or pgBouncer pool on Postgres. Skip to
     [DB exhausted](#5-database-connection-pool-exhausted).
   - Completes fast → not the DB; continue.

2. **Run queue depth.** `GET /v1/slo` shows `queue_depth`. If high
   (>50 for free tier, >500 for paid): a worker is hung or
   crashlooped. See [Worker hung](#2-worker-hung-or-stuck).

3. **One slow route or all of them?** Compare per-route p95 in
   `/v1/slo/latency`. If only a single route, look at recent PRs
   touching that handler. If everything: external dep regression
   (Anthropic, Stripe, GitHub).

**Resolve:**

- Roll back the offending PR (the symptom started shortly after a
  deploy; check `kubectl rollout history` or your CI deploy log).
- For external-dep regressions, set the relevant feature flag to
  fall back to a cached / degraded mode if available; otherwise
  status-page it (see [Status page](#7-status-page-update)).

---

## 2. Worker hung or stuck

**Recognise:** Run queue depth grows but `/v1/slo` shows
`runs_processed_last_hour` near zero. The
`claudestruct-alerts-scheduler` thread also stops emitting.

**Triage:**

1. `ps aux | grep "cs serve worker"` — is the process alive?
   - Dead → it crashed. `journalctl -u claudestruct.service -n
     200` should have the traceback.
   - Alive → check `py-spy dump --pid <PID>` for the stuck
     thread.

2. Inspect the in-flight run:
   ```bash
   cs serve runs list --status running
   ```
   Note the `run_id` and `created_at`. If it's been "running" for
   more than the tier's `max_runtime_seconds`, the runner ignored
   the timeout (real bug, file an issue).

**Resolve:**

- Restart the worker:
  ```bash
  systemctl restart claudestruct.service     # bare-metal
  kubectl rollout restart deployment/claudestruct-worker  # k8s
  ```
- The in-flight run will be re-claimed by the next worker. The
  per-run timeout protects against an infinite loop in the same
  payload.

**Prevention:** the `WorkerThread` already traps SIGINT/SIGTERM
and drains gracefully; if you find a path that bypasses it, that's
a real bug.

---

## 3. Stripe webhook backlog

**Recognise:**
`rate(claudestruct_webhook_errors_total{path="stripe.subscription_retrieve"}[5m])`
is non-zero, OR Stripe's dashboard shows webhook delivery failures
to our `/v1/billing/webhook`.

**Triage:**

1. Is Stripe healthy right now? https://status.stripe.com
2. Are we returning 5xx? Check the worker log:
   ```bash
   journalctl -u claudestruct.service | grep "stripe webhook"
   ```
3. Is `STRIPE_WEBHOOK_SECRET` correct? A rotated secret causes
   every webhook to 400 with "signature verification failed".

**Resolve:**

- If Stripe is degraded: nothing to do; their retry policy will
  re-deliver. Watch the alert clear.
- If our secret is wrong: update via the secrets manager and
  bounce the workers. Then ask Stripe support to **resend** the
  failed deliveries from the last hour (they expire after 3 days
  by default; replay before then).
- If we returned 500: open a P1, attach the traceback, and
  manually reconcile any subscriptions that landed during the
  outage by inspecting the `audit_entries` chain for missing
  `stripe.customer.subscription.updated` rows.

---

## 4. Cost-regression alert (single run)

**Recognise:** Slack / email message
`run X cost $Y is N σ above 30d mean`.

**Triage:**

1. Pull the run:
   ```bash
   cs serve runs show <run_id>
   ```
2. Why was it expensive?
   - Wrong model selected (e.g. `--effort xhigh` on a small task)
   - Context blew past `--max-bytes` and didn't shrink
   - Real regression in the prompt (recent prompt-template change)

**Resolve:**

- If the user mis-flagged the run: chat with them; no production
  action needed.
- If the prompt regressed: revert the prompt PR. The
  `prompt: dev v=...` line in the run summary tells you which
  version produced the run.
- If an org is repeatedly tripping the alert: bump them up a tier
  or add a per-org soft-cap exception in `tier_usd_cap()` after
  ops review.

---

## 5. Database connection pool exhausted

**Recognise:** `OperationalError: too many connections` (Postgres)
or `database is locked` (SQLite). API returns 5xx on every route.

**Triage:**

1. Postgres: `psql -c "SELECT count(*) FROM pg_stat_activity;"`
2. SQLite: `lsof | grep server.db | wc -l` — if >100, a worker
   forgot to close its session.

**Resolve:**

- Postgres: bounce pgBouncer if used; otherwise the only option is
  rolling-restart the daemon to drop dangling sessions.
- SQLite: planned migration to Postgres is the long-term fix
  (tracked under W8.1 in the project roadmap). Short-term: bounce
  the daemon.

After resolving: add a regression test that catches the path that
leaked the connection.

---

## 6. Cache hit-rate collapse

**Recognise:** Cost-regression alerts fire across multiple runs
in a short window AND `cs dashboard` shows
`cache.hit_rate < 50 %`.

**Triage:**

1. Did the system prompt change? Every byte of drift in the
   cached prefix invalidates the entire window. Check
   `prompt: dev v=...` against the previous good run.
2. Is `cache_control: ephemeral` still set? Grep
   `src/claudestruct/prompts.py` — if the cache marker is removed
   from a recently-edited prompt, every call pays full input
   cost.

**Resolve:**

- Roll back the prompt change. The prompt-version surface
  (W4.4-era content-hash) tells you the affected window.
- If the change was intentional, accept the temporary cost
  spike — Anthropic's 1 h cache TTL means the new prefix
  saturates within an hour.

---

## 7. Status page update

We don't yet host a public status page (W8.7 — external
`status.claudestruct.dev` deferred). Until that lands:

- Post in `#claudestruct-incidents` with a one-line summary +
  expected-resolution timestamp
- Update the in-app banner via the SPA's runtime config (the
  exact mechanism depends on your deploy — see your local
  `deploy/` notes)
- Email customers on `business` tier when an incident is
  customer-impacting

The decision about whether something is "customer-impacting" is
intentionally human — when in doubt, communicate.

---

## 8. Common diagnostic commands

```bash
# How healthy am I?
curl -s http://localhost:8000/healthz | jq
curl -s http://localhost:8000/v1/slo | jq

# Is the worker draining?
cs serve worker --once

# What's the audit chain showing recently?
curl -s -H "Authorization: Bearer ck_..." \
  http://localhost:8000/v1/audit?limit=20 | jq '.entries[]'

# Are webhook side-effects firing or silently failing?
curl -s http://localhost:8000/v1/slo/webhook_errors

# Re-verify the audit chain integrity (slow on big chains)
cs serve audit verify
```

## 9. When to escalate

Escalate to engineering oncall if:

- Customer data is at risk (cross-tenant leak, audit chain break)
- Production deploy needs to be rolled back outside of business
  hours
- An external dep (Anthropic, Stripe, GitHub) is reporting a
  multi-hour outage and customers are asking
- You're not sure — better to wake someone up than guess
