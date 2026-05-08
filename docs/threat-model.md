# Threat model

This document describes the security boundaries claudeStruct
operates within and the threats those boundaries are meant to
defend against. It uses STRIDE (Spoofing, Tampering, Repudiation,
Information disclosure, Denial of service, Elevation of privilege)
as the structuring lens.

Read [the security policy](security.md) first for the disclosure
process and the in-scope / out-of-scope list. This doc is the
*technical* counterpart — what we defend, what we don't, and why.

If you're a security researcher: the priority defects are the
ones that **violate an assumption stated below**. A finding that
matches a documented "out of scope" line is not a vulnerability
in our terms.

---

## Trust boundaries

```
   ┌─────────────────────────────────────────────────────────┐
   │                  External actors                        │
   │  - Anonymous: /healthz, /readyz, /status, /v1/slo*,     │
   │    /v1/auth/github/{login,callback}, /v1/billing/webhook│
   │  - Bearer:  /v1/runs, /v1/keys, /v1/dashboard, ...      │
   │  - Cookie:  /v1/auth/me, /v1/auth/logout                │
   └────────────┬────────────────────────────────────────────┘
                │  HTTPS  (terminated by the LB / ingress)
                ▼
   ┌─────────────────────────────────────────────────────────┐
   │                   Daemon process                        │
   │   FastAPI app  ──────────►  Worker thread               │
   │       │                         │                       │
   │       └────► Session factory ◄──┘                       │
   └────────────┬────────────────────────────────────────────┘
                │
                ▼
   ┌─────────────────────────────────────────────────────────┐
   │                    Data plane                           │
   │   SQLite / Postgres   |   Stripe (HTTPS)               │
   │   Anthropic API       |   GitHub App API                │
   │   Slack webhook       |   SMTP server                   │
   └─────────────────────────────────────────────────────────┘
```

The only assumed-trusted interior is the daemon process and the DB.
Everything else — including the LB, every external API, every
webhook, every run author — is treated as adversarial.

---

## STRIDE per surface

### S — Spoofing

| Threat                                            | Defence                                                                         |
| ------------------------------------------------- | ------------------------------------------------------------------------------- |
| Forged `Authorization: Bearer ck_...`             | Constant-time `sha256(secret)` compare; secret never written; rotate on revoke   |
| Forged session cookie                             | URL-safe random `session_token`; per-row `revoked_at`; 14-day hard expiry        |
| Forged Stripe webhook                             | `stripe.Webhook.construct_event` HMAC verify against `STRIPE_WEBHOOK_SECRET`     |
| Forged GitHub webhook (App)                       | HMAC-SHA256 over body with the App's webhook secret                              |
| Cross-org enumeration via 404 vs 403              | Tenant-scoped 404 contract (see "Tenant isolation" below)                        |
| OAuth state replay                                | CSRF state cookie; mismatch → 400; cookie cleared after callback                 |

### T — Tampering

| Threat                                              | Defence                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| In-flight request modification                      | HTTPS termination at LB; HSTS at LB; we don't accept `http://` callbacks         |
| Audit log row deletion                              | Hash-chained per-org chain; `cs serve audit verify` walks and reports first break |
| Skill marketplace tampering                         | sha256 + cosign-style signature verify (W7.3); `CLAW_SKILLS_REQUIRE_SIGNATURE=1` |
| Webhook payload tampering                           | Signed (Stripe HMAC, GitHub HMAC); reject on mismatch with no fallback           |

### R — Repudiation

| Threat                                              | Defence                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| "I never created that key"                          | `key.create` audit row at issuance time; `key.revoke` on revoke                  |
| "I never logged in"                                 | `auth.login.success` audit row written by OAuth callback                         |
| "I never paid"                                      | `stripe.invoice.payment_succeeded` audit row written by webhook                  |

### I — Information disclosure

| Threat                                              | Defence                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| Cross-tenant data leak via list endpoints           | Every list/get is `org_id`-scoped; `with_org_scope()` + `require_org_owned()`   |
| Cross-tenant id enumeration                         | 404 (not 403) on cross-tenant fetch — see "Tenant isolation"                     |
| Audit log leak across orgs                          | Per-org chain; admin-only read; audit `seq` is org-scoped not global             |
| Bearer token exposure in logs                       | Audit rows record key_id, never the secret; logger redacts on `--redact`         |
| Stripe customer-id enumeration                      | `/v1/billing/invoices/{id}/pdf` 404s on cross-tenant invoice                     |
| Status page leaking org names                       | `test_status_endpoint_no_tenant_data_leaks` asserts no org/email/run-id in body  |
| OAuth token storage                                 | Never persisted — exchanged for our own session token at callback time           |

### D — Denial of service

| Threat                                              | Defence                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| Run spam exhausts Anthropic budget                  | Per-tier `TIER_TOKEN_CAPS` enforced at run-submit; 402 Payment Required on cap  |
| Run spam exhausts concurrency                       | Per-tier `SandboxLimits.max_concurrent_runs`; tier-priority claim in worker      |
| Long-running run hangs the worker                   | Per-tier `SandboxLimits.max_runtime_seconds`                                    |
| Webhook flood overwhelms DB                         | Stripe + GitHub both retry, so a brief degraded path is acceptable                |
| Billion-row dashboard query                         | List endpoints cap `limit` (e.g. dashboard `?limit≤500`)                         |
| Empty `IN ()` SQL on team filter                    | Empty-team short-circuit in dashboard                                            |

**Out of scope:** classic L7 flood. Mitigation is the LB (rate
limit, WAF), not the daemon. We deliberately do not implement
in-process rate limiting because:
- the daemon scales horizontally; in-process limits would be wrong
  by a factor of N replicas
- the LB has visibility we don't (TLS handshakes, IPs, ASNs)

### E — Elevation of privilege

| Threat                                              | Defence                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| Viewer escalating to admin                          | `require_role(Role.admin)` dependency; ROLE_RANK lattice; tested in test_server  |
| Run executing arbitrary host commands               | `claw-sandbox` Go wrapper enforces rlimits + path validation                     |
| Plugin SDK escaping the orchestrator                | Plugins run inside the same process; trust model = "you ran the install"        |
| Skill install dropping arbitrary file               | Install path is `<repo>/.claw-squad/skills/<id>.md` only; no traversal           |
| Debug endpoint exposed in prod                      | No `/debug/*` routes exist; lazy-import the MCP server only when `cs mcp` runs   |

---

## Tenant isolation rules

Every multi-tenant query in the codebase uses one of two helpers:

- `auth_mod.with_org_scope(stmt, model, principal)` — adds
  `.where(model.org_id == principal.org_id)` to a SELECT
- `auth_mod.require_org_owned(row, principal, label=...)` —
  raises 404 if the row is None or has a different `org_id`

The 404 contract is non-negotiable: a row that exists in another
org must be **indistinguishable** from a row that doesn't exist
anywhere. 403 would tell an attacker the id is real in *some* org,
which is a low-key enumeration oracle. The status code MUST be 404
for both cases.

This rule is tested at every list / get / mutate site that touches
a tenant-scoped row (keys, runs, invoices, teams, etc.).

---

## Defence in depth

Even when the boundary above holds, the daemon has secondary
defences:

- **Audit chain** — Every state-changing call writes an
  audit row. A tampering attacker would need to compromise the DB
  AND keep the chain hashes consistent. `cs serve audit verify`
  detects single-row tampering.
- **Sandbox** — The Go `claw-sandbox` wrapper sets rlimits + path
  validation + env scrubbing on every subprocess. macOS and
  unprivileged Linux can't enforce `--no-network` (that's a
  documented limitation in the `[sandbox] {"event":"isolation",...}`
  JSON line emitted at startup); operators who need stronger
  guarantees run inside Docker / firejail.
- **Webhook errors counter** — Silent-swallow paths bump
  `claudestruct_webhook_errors_total{path,reason}` so a stealthy
  failure shows up in `/v1/slo/webhook_errors` even if no log line
  is read.
- **Burn-rate alerting** — `compute_burn_rate_alerts` (Google SRE
  multi-window) yells before an org crosses its monthly error
  budget; on-call gets a head start vs. the post-mortem.

---

## Known limitations

These are accepted risks, not unresolved findings:

- **No L7 rate limiting in-process** — see DoS row above
- **macOS/unprivileged-Linux sandbox** — `--no-network` requires
  `CAP_SYS_ADMIN`; we don't claim to deliver it on those
  platforms. The startup JSON line tells operators what was
  actually configured
- **Public `/status` is unauthenticated** — payload is global
  aggregates only; the test suite enforces the invariant
- **Per-run Docker container isolation deferred** — current
  sandbox is rlimits + path validation; W8.3 follow-up adds the
  per-run Docker layer for tier-based hard isolation

---

## Reviewing security-relevant changes

A PR that touches any of the following surfaces is a "security
review" PR — flag it in the description and request a second
reviewer:

- `server/auth.py` (any change)
- `server/routers/oauth.py` (any change)
- `server/audit.py` (any change to chain logic)
- `server/routers/billing.py` webhook handler
- `server/github_app/` (new package; webhook + JWT signing)
- `claw-squad/src/skills-registry.ts` signature path
- `claw-sandbox/` (any change)
- A new outbound HTTP destination from the daemon
- Any 4xx/5xx mapping change that could leak resource existence

When in doubt: ask. The cost of a second reviewer is much lower
than the cost of a documented invariant being silently broken.
