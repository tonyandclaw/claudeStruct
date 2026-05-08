# Admin event timeline

The hosted-side daemon writes a per-org append-only audit chain
(W8.4) for every admin- or auth-relevant action. Support, on-call,
and admins read this chain when answering "who did what when" — the
single timeline that survives even after a session ends or a key
rotates.

`GET /v1/audit` (admin only) returns the chain for the caller's org.
Each row carries:

- `seq` — strictly monotonic per org
- `actor_user_id` — null for system actions
- `action` — the event name (table below)
- `resource_type` + `resource_id` — what was acted on
- `payload_json` — extra detail (no secrets — see "PII rules" below)
- `prev_hash` + `entry_hash` — hash chain for tamper detection

## Event catalogue

| `action`                                 | When it fires                                                         | `resource_type`     | `payload_json`          |
| ---------------------------------------- | --------------------------------------------------------------------- | ------------------- | ----------------------- |
| `auth.login.success`                     | OAuth callback successfully creates a `UserSession` row               | `user_session`      | `{provider}`            |
| `auth.logout`                            | `POST /v1/auth/logout` revokes a previously-active session            | `user_session`      | `{provider}`            |
| `key.create`                             | Admin calls `POST /v1/keys`                                           | `api_key`           | `{name}`                |
| `key.revoke`                             | Admin calls `DELETE /v1/keys/{id}`                                    | `api_key`           | `{}`                    |
| `run.submit`                             | Run posted via `POST /v1/runs`                                        | `run`               | (truncated run intent)  |
| `billing.checkout.create`                | Admin starts a Stripe Checkout session                                | `checkout_session`  | `{tier}`                |
| `stripe.checkout.session.completed`      | Stripe webhook confirms the session finished                          | `subscription`      | (Stripe event id)       |
| `stripe.customer.subscription.updated`   | Tier change / period renewal                                          | `subscription`      | (Stripe event id)       |
| `stripe.customer.subscription.deleted`   | Subscription cancelled                                                | `subscription`      | (Stripe event id)       |
| `stripe.invoice.payment_succeeded`       | Invoice paid                                                          | `subscription`      | `{invoice_id}`          |
| `stripe.invoice.payment_failed`          | Invoice failed                                                        | `subscription`      | `{invoice_id, reason}`  |

(Future events land here as they're added — see PRs touching
`server/audit.py` callers.)

## PII rules

The chain is read by support — keep it useful and safe:

- **Never** record bearer tokens, OAuth state cookies, session
  tokens, API key secrets, or Stripe webhook signatures. The audit
  row carries the row id (e.g. the `UserSession.id`) so support can
  correlate without exposing the credential.
- Redact email addresses if the org's tier requires it (free tier
  keeps emails; tested for `team`/`business`).
- Stripe event ids are safe — they're public per Stripe's docs.

## Reading the chain

```bash
# For an admin in your own org:
curl -H "Authorization: Bearer ck_..." \
  https://api.claudestruct.dev/v1/audit?limit=200 \
  | jq '.entries[] | {seq, action, resource_id, created_at}'
```

The chain is immutable from the outside — `entry_hash` covers the
preceding `prev_hash`, so any back-fill or row deletion is detectable
by `cs serve audit verify` (which walks the chain and recomputes).
