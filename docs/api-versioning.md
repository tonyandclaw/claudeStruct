# API versioning + deprecation policy

This is the contract between the claudeStruct hosted API and every
client (CLI, MCP server, dashboards, plugin SDK, third-party
integrations). Read it before mutating any handler under
`src/claudestruct/server/routers/`.

## What's versioned

The path prefix is the major version. Today every public route
ships under `/v1/...`. A response carries two confirming headers:

```http
X-CS-Api-Version: v1
X-CS-Region: us-east-1
```

A client built against these docs should assert both before
trusting the body — a misconfigured proxy that strips the prefix
will still fail the version-header check.

## What changes within a major version

These changes are **safe** within `/v1` (don't bump):

- Adding a new route
- Adding a new optional response field
- Adding a new optional request field with a sane default
- Adding a new query parameter
- Loosening a validation rule
- Adding a new audit event kind, alert kind, or notification provider
- New status codes for previously unhandled error paths
  (e.g. introducing 409 where 404 used to fire), as long as the
  4xx → 4xx and 5xx → 5xx side stays consistent

These changes are **breaking** and require `/v2`:

- Removing a field from a response (clients may parse it)
- Renaming a field (same risk)
- Changing the type of a field (`int` → `string`, etc.)
- Removing a route or method
- Tightening a validation rule that previously-valid bodies fail
- Changing the meaning of an HTTP status code on a happy path
- Changing the auth contract (cookie name, bearer prefix, header
  name)
- Changing the default value of an existing optional field

When in doubt: a breaking change is anything that would require a
released CLI version to be re-built to keep working.

## How `/v2` lands

1. Mount the new router under `/v2/...` next to `/v1/...`. The
   FastAPI app keeps both mounted simultaneously — a single
   deployment serves every supported major.

2. Change `X-CS-Api-Version` to be route-specific. The simplest
   implementation: switch `ApiVersionHeaderMiddleware` to read
   `request.scope["root_path"]` or `request.url.path` and pick
   whichever prefix matched.

3. Add `Deprecation: true` and `Sunset: <RFC9111 date>` headers to
   every `/v1` response. The deprecation window is **at least 6
   months** for paying customers, **at least 3 months** for free
   tier (per the SLA in `docs/slo.md`). The CHANGELOG entry that
   ships `/v2` MUST cite the sunset date.

4. The CHANGELOG calls out the migration: which fields moved, the
   one-page diff, and the recommended migration step (sed, codemod,
   or "no change needed because the SDK insulates you").

5. After the sunset date, `/v1` returns `410 Gone` for one minor
   release before the routes are removed. The `410` body still
   includes the migration link.

## Per-route deprecation (within a major)

A single endpoint can be deprecated without bumping the major.
Stamp it via response header:

```http
Deprecation: true
Sunset: Wed, 01 Jan 2027 00:00:00 GMT
Link: <https://docs.claudestruct.dev/api-migration-2027>; rel="deprecation"
```

The CHANGELOG records the deprecation. After the sunset date the
endpoint returns `410 Gone` with the same `Link` header.

## What clients should do

1. Parse `X-CS-Api-Version` on every response. Surface a warning
   when the server returns a higher major than the client
   targets — the contract may have shifted under you even within
   a deployment.

2. Treat `Deprecation: true` as a soft alarm. Log it; don't
   crash. The `Sunset` header tells you how long you have to
   migrate.

3. Don't pin to a specific minor release in URL space. Minor
   releases ship via the same `/v1` path; the version header
   surfaces minor diffs only on the OpenAPI spec
   (`GET /openapi.json`).

## Where to look in the code

- `server/app.py` — `API_VERSION` constant + `ApiVersionHeaderMiddleware`
- `server/routers/*.py` — every public endpoint mounts its own
  prefix; the `prefix="/v1"` lives in each `APIRouter(...)` call
  rather than centrally so a future `/v2` can mount cleanly
- `docs/changelog.md` — minor + breaking diffs land here
