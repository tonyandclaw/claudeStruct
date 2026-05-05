# claudeStruct Roadmap TODO

Tracks the wave-by-wave roadmap. Updated on every PR push.

Legend: `[ ]` pending · `[~]` in progress · `[x]` done

---

## Wave 1 — Quick Wins

- [x] **W1.1 — Cache hit rate alarm** (B4)
  - claudestruct: `src/claudestruct/cache_state.py` persists `(task, model, prompt_hash)` → write timestamp under `~/.claudestruct/cache_state.json`; CLI surfaces a `[warning]` line when the next call within TTL reads 0 cached tokens
  - claw-squad: `isSilentCacheInvalidator` in `claw-squad/src/totals.ts` already wired into `printSummary` (cli.ts:577)
  - Tests: `tests/test_cache_state.py` (7 cases)
- [x] **W1.2 — Ctrl+C / cancellation handling** (A2)
  - `claw-squad/src/abort-signal.ts` wraps the UI: 1st SIGINT → existing `onQuit` → `persistState` runs in orchestrator's `finally`; 2nd SIGINT → exit 130
  - Wired in `claw-squad/src/cli.ts` around `runOrchestrator`
  - Tests: `claw-squad/tests/abort-signal.test.ts` (5 cases)
- [x] **W1.3 — claw-squad `--dry-run`** (C1)
  - `claw-squad/src/dry-run.ts` heuristic estimator (per-todo Coder + Reviewer with avg token sizes × `min(maxReviewRounds, 1.3)`)
  - Orchestrator short-circuits after Phase 2 with `reason: "dry_run"`
  - CLI flag wired; `dry_run` exits 0
  - Tests: `claw-squad/tests/dry-run.test.ts` (9 cases)
- [x] **W1.4 — Prompt versioning** (C4)
  - Content-hash auto-versioning (sha256 prefix) — no human bumping required
  - claudestruct: `prompt_version()` + `TASK_PROMPT_VERSIONS` in `src/claudestruct/prompts.py`; CLI prints `prompt: dev v=abc12345`
  - claw-squad: `loadPromptVersion()` in `src/prompts.ts`; printed in `printSummary` for all 3 roles
  - Tests: `tests/test_prompts.py` (4 cases) + `claw-squad/tests/prompts.test.ts` (4 cases)
- [x] **W1.5 — Sandbox isolation transparency** (A4)
  - `claw-sandbox/isolation_report.go` emits one structured JSON line on every run: `[sandbox] {"event":"isolation","platform":"darwin","rlimitCpu":"unsupported",...}`
  - Platform-specific `isolationCapabilities()` in `rlimit_linux.go` / `rlimit_other.go`
  - `claw-squad/README.md` documents per-platform caveats (Linux: rlimits enforced, no-network unsupported; macOS/other: most controls unsupported, defense-in-depth only)
  - Tests: `claw-sandbox/isolation_report_test.go` (4 cases) — needs Go toolchain to run

### Wave 1 verification

| Suite | Result |
|---|---|
| `pytest tests/test_cache_state.py tests/test_prompts.py` | 11 passed |
| `npx vitest run` (claw-squad) | 220 passed (22 files) |
| `npx tsc --noEmit` | clean |
| `go test ./claw-sandbox/...` | not run locally (no Go); expected to pass on CI |

---

## Wave 2 — Foundations

- [x] **W2.1 — Structured logging** (B1) ⚓ basis for B2/B3/D1
  - claudestruct: `src/claudestruct/logging.py` event sink (run.start / agent.usage / cache.warning / run.end), `src/claudestruct/cost.py` Anthropic rate table; new `--log-json <path>` flag
  - claw-squad: `RunLogHandle.mirrorPath` extends `src/runs/log.ts`; `RunConfig.logJsonPath` threaded through orchestrator; new `--log-json <path>` CLI flag
  - Tests: `tests/test_logging.py` (8 cases) + `claw-squad/tests/runs.test.ts` (+2 cases for mirror)
- [x] **W2.2 — API retry + timeout** (A1)
  - claudestruct: `_env_float` / `_env_int` helpers; `anthropic.Anthropic(timeout=, max_retries=)` with `CLAUDESTRUCT_TIMEOUT` / `CLAUDESTRUCT_MAX_RETRIES` env-var overrides (defaults 300s / 3)
  - claw-squad: `src/providers/transport.ts` with `CLAW_SQUAD_TIMEOUT` / `CLAW_SQUAD_MAX_RETRIES`; both Anthropic and OpenAI-compat clients now pass `timeout`/`maxRetries` to their SDKs
  - Tests: `tests/test_client_env.py` (6 cases) + `claw-squad/tests/transport.test.ts` (5 cases)
- [x] **W2.3 — Unified config schema + validation** (C3)
  - `claw-squad/src/config-schema.ts` with zod schemas for the full `.claw-squad/config.json` shape (strict objects → unknown-field rejection; field-path errors like `agents.planner.effort`)
  - Hooked into `readConfigFile` so the file is gated before merge
  - Added zod ^3.25 to package.json
  - Tests: `claw-squad/tests/config-schema.test.ts` (9 cases)
- [x] **W2.4 — Slack / Web UI reconnect** (A3)
  - Slack Socket Mode: subscribe to SDK `disconnected` / `reconnecting` / `connected` events; expose `onConnectionState(fn)` on `SocketReplyStrategy`; `slack.ts` posts `:warning: lost socket — reconnecting…` and `:white_check_mark: reconnected` to the channel
  - Web UI: client-side reconnect now uses jittered exponential backoff capped at 30s with retry counter shown in the connection banner
  - Tests: `tests/slack-socket.test.ts` (+3 cases for connection state)
- [x] **W2.5 — Architecture / contributor docs** (C2)
  - `CLAUDE.md` at repo root: layout + how to run + key conventions
  - `claw-squad/docs/agents.md` — orchestrator state machine diagram, agent contract, UI seam, adding a new agent role
  - `claw-squad/docs/skills.md` — skill format, two activation paths (Planner-tagged vs `apply_to`), authoring tips
  - `claw-squad/docs/hooks.md` — lifecycle hooks surface, `HookAbort`, recipes
  - `claw-squad/docs/providers.md` — adding a new provider (OpenAI-compat path vs new client class)

### Wave 2 verification

| Suite | Result |
|---|---|
| `pytest tests/` (Python) | 25 passed (cache_state + prompts + logging + client_env) |
| `npx vitest run` (claw-squad) | 239 passed (24 files; +19 new tests) |
| `npx tsc --noEmit` | clean |

---

## Wave 3 — Advanced Capabilities

- [x] **W3.1 — claudestruct dashboard parity** (B3)
  - `src/claudestruct/dashboard.py` folds JSONL events into `RunSummary`; new `cs dashboard` subcommand renders a Rich table with `--task` / `--json` / `--limit` filters
  - Run logs now auto-write to `<root>/.claudestruct/runs/<ts>.jsonl` via the new `MultiSink` / `fanout_log` helpers in `logging.py` (preserves the `--log-json` mirror)
  - Tests: `tests/test_dashboard.py` (7 cases) — round-trips writer → reader, malformed-line skipping, `--task` / `--since` filters, JSON shape parity
- [x] **W3.2 — Per-task token budget tuning** (D4)
  - `BUDGETS_PER_TASK` in `src/claudestruct/context.py`: review 200k, dev 600k (=`DEFAULT_MAX_TOTAL_BYTES`), debug 400k, plan 800k
  - Each gatherer now defaults to its task-specific budget; CLI flag `--max-bytes` overrides
  - Tests: `tests/test_budgets.py` (5 cases) — pins per-task ordering and unknown-task fallback
- [x] **W3.3 — Cross-run agent memory v2** (D2)
  - Verified: `src/memory/memory.ts` already wired (Planner reads tail before each run, lessons accumulate after each task) — base behavior was working
  - Added `readRelevantMemorySnippet(repoRoot, query, budgetBytes?)`: keyword-overlap scoring (stopword-filtered, ≥3-char tokens) over per-lesson blocks; selects highest-scoring lessons within budget, falls back to chronological tail when query/lessons empty or all-zero scores
  - Orchestrator now passes `requirement` as the query at all three Planner call sites
  - Tests: `claw-squad/tests/memory.test.ts` (+5 cases for ranking, fallback, byte budget, stopword filtering)
- [x] **W3.4 — Metrics export (Prometheus text format)** (B2)
  - Chose Prometheus textfile collector over OTel SDK to keep the dep footprint at zero
  - `src/claudestruct/metrics.py` aggregates `<root>/.claudestruct/runs/*.jsonl` into Prometheus exposition: `claudestruct_runs_total`, `claudestruct_tokens_total{direction}`, `claudestruct_cost_usd_total`, `claudestruct_cache_warnings_total`, plus `claudestruct_last_run_*` gauges
  - New `cs metrics [--out <path>]` subcommand; suitable for node_exporter's textfile collector via cron
  - Tests: `tests/test_metrics.py` (6 cases) — counter sums, last-run gauges, label escaping, format-grammar regex
- [x] **W3.5 — Pre-commit / GitHub Actions integration** (D1)
  - `src/claudestruct/integrations/pre-commit-cs-review.sh` — staged-diff hook with diff-size cap, `CS_HOOK=0` bypass, critical-finding gating via `CS_HOOK_BLOCK`
  - `src/claudestruct/integrations/github-action-cs-review.yml` — drop-in workflow that runs `cs review` on PRs and posts verdict as a comment
  - `src/claudestruct/integrations/README.md` — install + env-var reference
- [~] **W3.6 — Incremental context shrinking** (D3) — DEFERRED
  - **Status**: explicitly deferred per the original plan ("先量化收益再決定是否做")
  - **Why**: Anthropic's 1h prompt cache already covers the common path. The marginal value of a hand-rolled file-hash cache only kicks in when (a) cache TTL has expired (>1h between calls) AND (b) files actually haven't changed. We don't yet have telemetry showing this case is hot.
  - **Reopen criteria**: when the dashboard (`cs dashboard --json`) or the metrics export (`cs metrics`) shows >1h-elapsed call patterns dominating spend, revisit with concrete numbers.
- [~] **W3.7 — Multi-repo conflict resolution** (D5) — DEFERRED
  - **Status**: explicitly deferred per the original plan ("建議在前 6 項做完、有真實 multi-repo 使用 case 之後再 design")
  - **Why**: cross-repo TODO routing already works (Planner tags `repoAlias`; orchestrator dispatches to the right `RepoCtx`). Conflict-resolution semantics — what to do when a TODO in repo A and a TODO in repo B both modify a shared API contract — depend on workflow conventions that vary per team. Designing without a real reproducer risks shipping the wrong abstraction.
  - **Reopen criteria**: a concrete failure mode observed in production multi-repo runs, with the desired resolution behavior named.

### Wave 3 verification (cumulative)

| Suite | Result |
|---|---|
| `pytest tests/` (Python) | 51 passed (+6 new for metrics) |
| `npx vitest run` (claw-squad) | 254 passed (+6 new for memory v2) |
| `npx tsc --noEmit` | clean |

---

## Roadmap status

✅ **Wave 1** (5/5) — quick wins
✅ **Wave 2** (5/5) — foundations
✅ **Wave 3** (5/7 done; 2 explicitly deferred with reopen criteria)
🚧 **Wave 4** (Q1) — public release readiness
⏳ **Wave 5** (Q2) — production hardening
⏳ **Wave 6** (Q3a) — team collaboration
⏳ **Wave 7** (Q3b) — ecosystem & GTM
⏳ **Wave 8** (Q4) — hosted SaaS launch

The 1-3 roadmap is closed. Wave 4-8 below tracks the path to a "complete and commercializable" product. Target shape: **OSS dev tool + hosted SaaS (open core)**, **teams of 5-50 devs**, **~12 months**.

---

## Wave 4 — Public release readiness (Q1)

Goal: anyone can `pip install claudestruct` / `npm install claw-squad` / `docker run` and get a working, trustworthy tool. Today everything ships unsigned, untested-by-CI, undocumented past `claw-squad/docs/`.

- [x] **W4.1 — GitHub Actions CI matrix**
  - `.github/workflows/ci.yml` — pytest (3.10/3.11/3.12/3.13), vitest + tsc (Node 20/22), go test + go vet (1.22)
  - Concurrency group cancels superseded runs; pip/pnpm caches keyed off lockfiles
  - ruff lint deferred (pyproject.toml has no ruff config yet; tracked under W5)
- [x] **W4.2 — License + governance files**
  - `LICENSE` (MIT, matches `pyproject.toml` declared license) at repo root
  - `CONTRIBUTING.md`, `SECURITY.md` (vuln disclosure)
  - `CHANGELOG.md` (keepachangelog 1.1 format) seeded with Waves 1-3 history
  - `CODE_OF_CONDUCT.md` — official Contributor Covenant 2.1 fetched from `contributor-covenant.org`; `[INSERT CONTACT METHOD]` swapped to point at `SECURITY.md`
- [~] **W4.3 — Release automation**
  - `.github/workflows/release.yml` — on `v*.*.*` tag push: PyPI sdist+wheel via OIDC trusted publishing, npm publish (`claw-squad`) with `--provenance`, GitHub Release with cross-compiled `claw-sandbox` binaries (linux/darwin × amd64/arm64) + aggregated `SHA256SUMS`
  - Workflow uses `env:` block routing for every shell-substituted ref to keep template injection out of `run:` bodies
  - Trusted-publishing config (PyPI project + npm package settings) is the remaining manual step before the first tag
- [x] **W4.4 — Container distribution**
  - Multi-stage `Dockerfile` (python-slim base, Go builder for sandbox, Node builder for claw-squad) → `ghcr.io/tonyandclaw/claudestruct:latest`
  - `.github/workflows/docker.yml` builds on every PR (verifies the Dockerfile) and pushes to GHCR on push to main + on tag, with PR/branch/sha tags via `docker/metadata-action`
  - SBOM (syft) + vuln scan (trivy) deferred to a follow-up PR alongside W4.3
- [x] **W4.5 — Docs site**
  - mkdocs-material wraps `claw-squad/docs/*.md` + the root README/CHANGELOG/CONTRIBUTING/SECURITY/TODO via `mkdocs-include-markdown-plugin` (single source of truth, no duplicated content)
  - `.github/workflows/docs.yml` builds on every PR (`mkdocs build --strict`) and deploys to GitHub Pages on push to main via `actions/deploy-pages@v4`
  - GH Pages must be enabled at the repo level (manual step) for the deploy job to land; build job runs unconditionally
- [~] **W4.6 — README polish + demo**
  - CI / Docs / License / Container badges added to the README header
  - Asciinema recording of `cs dev` and `claw-squad run` in action — pending (needs an env with API key)
  - Per-platform install (Homebrew, scoop, snap) tracked under W7.7

---

## Wave 5 — Production hardening (Q2)

Goal: trust this in CI pipelines and long-running daemons. Wave 4 makes it installable; Wave 5 makes it operable.

- [x] **W5.1 — OpenTelemetry tracing** (claudestruct + claw-squad)
  - claudestruct: `src/claudestruct/tracing.py` — opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`; soft dep on `opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-http` (declared as `claudestruct[otel]` extra in `pyproject.toml`); zero-cost no-op spans when disabled
  - claudestruct: `runner.run_task_and_log` wraps a root `claudestruct.task` span with child `claudestruct.context_gather` + `claudestruct.llm_call` spans; attributes: `task`, `model`, `prompt_version`, `budget_bytes`, `files`, `total_bytes`, `input_tokens`, `output_tokens`, `cache_*_tokens`, `cost_usd`, `duration_ms`
  - claw-squad: `claw-squad/src/tracing.ts` — same opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`; soft deps via `optionalDependencies` block in `package.json` (`@opentelemetry/api` / `sdk-node` / `exporter-trace-otlp-http`); dynamic-imported so default install stays lean
  - claw-squad: orchestrator wraps the full `runOrchestrator` execution in a root `clawSquad.run` span (attributes: `requirement` (200-char truncated), `maxLoops`, `maxReviewRounds`, `repoRoot`, then `reason`/`costUsd`/`calls` on completion); flushes via `shutdownTracing()` in the existing `finally` block
  - Tests: `tests/test_tracing.py` (6 cases for Python) + `claw-squad/tests/tracing.test.ts` (7 cases for TS) — disabled-by-default, idempotent init, no-op span methods, exception recording + span end on throw, attribute serialization, injection point for downstream tests, shutdown safety
- [x] **W5.2 — Error reporting (Sentry)**
  - `src/claudestruct/sentry_init.py` — opt-in via `CLAUDESTRUCT_SENTRY_DSN`; soft dep on `sentry-sdk` (declared as `claudestruct[sentry]` extra); init at CLI import so Click parsing errors are caught too
  - `before_send` scrubber redacts: env-var keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GITHUB_TOKEN` / `SLACK_*_TOKEN` / `CLAW_WEB_TOKEN` etc.) in `extra` / `tags` / `contexts` / `request.env`; `Authorization` / `X-API-Key` / `Cookie` headers in both dict and list-form serializations; nested dicts recursively; `<key>=<value>` patterns in breadcrumb messages
  - `traces_sample_rate=0.0` (use OTel for perf) and `send_default_pii=False`
  - Tests: `tests/test_sentry_init.py` (10 cases) — disabled path, idempotent init, every redaction surface (top-level / nested / request env / headers in both shapes / breadcrumb data + message), pattern coverage, explicit DSN argument override
- [x] **W5.3 — PII redaction + retention** (claudestruct only)
  - `src/claudestruct/redact.py` — `Redactor` walks event dicts replacing matched substrings with `[redacted]`. Default ruleset covers Anthropic / GitHub / Slack / Stripe tokens, AWS access keys, emails, JWTs. Pluggable via `Redactor.add_rule(name, pattern)`.
  - Wired through `EventSink` / `MultiSink` / `event_log` / `fanout_log`. CLI flag `--redact` (also `CLAUDESTRUCT_REDACT` env) constructs `Redactor.default()` and threads it to `run_task_and_log`.
  - `cs logs purge --older-than-days N [--dry-run]` uses `redact.purge_runs()` to delete `<root>/.claudestruct/runs/*.jsonl` files older than the cutoff (mtime-based for clock-skew safety).
  - Tests: `tests/test_redact.py` (21 cases) — every default rule + nested dict/list walk + non-string passthrough + custom rule + integration with EventSink + 5 purge cases (empty dir, recent skipped, old deleted, dry-run preserves, non-jsonl ignored)
  - claw-squad-side equivalent shipped: `claw-squad/src/runs/purge.ts` + `claw-squad runs list / purge --older-than-days N [--dry-run]`. Same mtime-based semantics as `cs logs purge` so retention rules stay in lockstep across both tools. Tests: `claw-squad/tests/runs-purge.test.ts` (7 cases — missing dir, no-match, old-only deletion, dry-run preserves, non-jsonl ignored, injectable `now`, daysToMs floor).
- [x] **W5.4 — Secrets vault integration** (claudestruct only)
  - `src/claudestruct/secrets.py` — `SecretsProvider` Protocol with built-in `EnvProvider`, `KeyringProvider`, `PassProvider`, `FileProvider`. Provider chain is selected by `CLAUDESTRUCT_SECRETS_PROVIDER` (e.g. `env,keyring,pass,file:/run/secrets`); first-hit-wins. Default `env`-only for back-compat.
  - `client.py:_make_client()` now reads via `secrets.get("anthropic.api_key")`. Legacy `ANTHROPIC_API_KEY` env still works (mapped via `_LEGACY_ENV_MAP`).
  - Tests: `tests/test_secrets.py` (24 cases) — env canonical / legacy / precedence / empty-as-miss; file provider read / strip / missing / empty; pass provider no-binary / first-line / nonzero-exit; keyring no-module fallback; default_chain selection / unknowns skipped / empty falls back; get/require semantics; client integration
  - Pending: AWS Secrets Manager + HashiCorp Vault providers (deferred; the abstraction is ready, the implementations need their respective SDKs as optional extras)
- [x] **W5.5 — Hardened sandbox**
  - `claw-sandbox/seccomp.json` — Docker / OCI / Kubernetes-compatible profile. `defaultAction=ALLOW` plus an explicit `ERRNO=1` denylist for ptrace + kernel modules (init/finit/delete) + kexec/reboot + mount/pivot_root/chroot + setuid/capset escalation + sethostname/clock-set + ioperm/iopl/bpf + unshare/setns + swapon/quotactl + add_key/keyctl/perf_event_open
  - `claw-sandbox/apparmor.profile` — sample profile mediating filesystem access. Allows /usr/lib + /lib + /workspace + /tmp + /etc/resolv.conf etc.; explicit deny for /etc/shadow, /root, ~/.ssh, ~/.aws/credentials, ~/.config/gh, /sys/kernel/{debug,tracing}, /dev/{mem,kmem,port}
  - `claw-sandbox/rlimit_linux.go` — `detectNetworkIsolationStatus()` probes uid (root → "enforced") and `/proc/self/uid_map` (unprivileged userns → "best-effort"); init namespace as non-root → "unsupported". Status flows into the existing structured isolation report so callers see the actual capability instead of a hardcoded "unsupported"
  - `claw-squad/docs/sandbox-hardening.md` — Docker / Kubernetes / firejail recipes, network-isolation guidance ("enforce outside the sandbox"), explicit list of out-of-scope cases (GPU, nested containers, side channels)
  - Tests: `claw-sandbox/isolation_report_test.go` updated to accept any of the three valid noNetwork status values since detection is now host-dependent
- [x] **W5.6 — Cumulative cost cap**
  - `src/claudestruct/budget.py` — `current_period_spend(root)` folds `<root>/.claudestruct/runs/*.jsonl` for the current UTC calendar month; `check_budget(root, cap)` returns `BudgetStatus(spent, cap, warn_threshold, exceeded, near_limit)` with `WARN_FRACTION = 0.8`
  - CLI: new `--monthly-cap-usd <float>` flag on `cs dev/review/plan/debug` (also reads `CLAUDESTRUCT_MONTHLY_CAP_USD`); hard-aborts (`exit 2`) before any LLM call when `spent ≥ cap`, soft-warns when `spent ≥ 0.8 × cap`. Skipped on `--dry-run`.
  - Tests: `tests/test_budget.py` (11 cases) — month bounds incl. December roll-over, period filtering, naive ISO timestamps, exact-threshold semantics, `cap=0` disables check, malformed run skipped
- [~] **W5.7 — Test rigor**
  - Ruff lint config in `pyproject.toml` (select F/E/W/I/B/UP/SIM, opinionated rules silenced); CI runs `ruff check src/ tests/` before pytest
  - Coverage gate via `coverage` (`fail_under = 70`; current run hits 80% with CLI/MCP entry points excluded as integration-tested)
  - CI workflow installs `pytest coverage ruff` and runs lint → coverage-gated pytest
  - Pending: end-to-end tests with mocked Anthropic SDK; mutation testing (mutmut / stryker) — both deferred until the unit-test floor is solid

---

## Wave 6 — Team collaboration (Q3a)

Goal: 5-50 devs share the tool with shared visibility, shared budgets, and team-level admin. Today the orchestrator and dashboard are single-user-on-this-machine.

- [x] **W6.1 — Daemon mode** (claudestruct only; claw-squad worker deferred)
  - `Run` SQLAlchemy model added to `src/claudestruct/server/models.py` with `RunStatus` enum (queued → running → done|failed). Per-row payload (task, description, model, effort, paths_json) plus outcomes (cost_usd, tokens, duration_ms, error)
  - `src/claudestruct/server/worker.py` — `process_pending_run(session, run_root, runner=...)` synchronous drain helper, `WorkerThread` long-running daemon thread with poll-interval + graceful stop, `drain_queue(...)` for cron / batch operation, `wait_until_empty(...)` test helper
  - `POST /v1/runs` now writes a queued `Run` row tagged with `org_id`/`user_id` instead of returning a placeholder id; `GET /v1/runs/{id}` reads from the DB first (with tenant isolation: cross-org lookup → 404, not 403, to avoid leaking existence) and falls back to the legacy JSONL log so pre-W6.1 history stays accessible
  - New `cs serve worker [--once] [--poll-interval N]` CLI subcommand (foreground daemon thread + Ctrl-C graceful drain, or single drain pass)
  - claw-squad-side daemon mode tracked separately — orchestrator's threading model + multi-repo state make it a bigger reshape
- [~] **W6.2 — HTTP REST API** (draft shipped)
  - FastAPI app under `src/claudestruct/server/` behind the `[server]` extra: `/healthz`, `/readyz`, `/v1/dashboard`, `/v1/budget`, `/v1/runs` (POST + GET), `/v1/keys` (list/create/revoke). OpenAPI 3.1 at `/openapi.json`, interactive viewer at `/docs`.
  - Auth: bearer API keys (`ck_<key_id>_<secret>`, SHA-256-hashed secret, last_used stamp on auth success).
  - `POST /v1/runs` returns 202 with a placeholder run_id — actual worker model lands in **W6.1**. Other endpoints work end-to-end against the existing JSONL store + budget module.
  - Tests: `tests/test_server.py` (18 cases) — auth gate (4), RBAC (3), key lifecycle (1), tenant isolation (2), dashboard / budget / runs shape (5), OpenAPI (1), health (2)
  - Pending: session cookies + browser SDK (deferred to W6.4 OAuth), per-language SDK stubs (deferred until the API surface is closer to final)
- [~] **W6.3 — User / team / org model + RBAC** (schema + key lifecycle shipped)
  - SQLAlchemy 2.x models: `orgs`, `users`, `memberships`, `api_keys`. Idempotent `init_db()` via `Base.metadata.create_all` for the draft; Alembic deferred until the first schema bump
  - Roles: `admin` / `member` / `viewer` enforced by `require_role(min_role)` FastAPI dependency
  - `cs serve init-db / add-org / add-user / add-key` covers the bootstrap path
  - Pending: teams (currently flat membership of users → orgs), seeded migration fixtures, Alembic when the schema needs to change shape
- [~] **W6.4 — OAuth login** (GitHub shipped; Google deferred)
  - `UserSession` SQLAlchemy model: per-row `session_token` (URL-safe random), `provider`, `expires_at` (14d hard cap), `revoked_at` for logout
  - `src/claudestruct/server/oauth.py` — pure helpers: `load_github_config()` reads env (`CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_ID` / `_SECRET` / `_OAUTH_REDIRECT_BASE`); `build_authorize_url`, `exchange_code_for_token`, `fetch_github_user` (with `/user/emails` fallback when the user's email is private). Injectable `http_client` so tests don't hit GitHub
  - `routers/oauth.py`: `GET /v1/auth/github/login` (CSRF state cookie + redirect), `GET /v1/auth/github/callback` (state verify, token exchange, user fetch, session mint, HTTPOnly+Lax+Secure cookie); `GET /v1/auth/me` (cookie-driven principal); `POST /v1/auth/logout` (revoke + clear). 503 when env vars unset; 403 (not auto-provision) on unknown email — admin must `cs serve add-user` first to avoid the "any GitHub account in the world creates a tenant" footgun
  - `auth.py:current_principal` chain: bearer wins, then session-cookie fallback. New `authenticate_session_cookie(session, cookie)` mirrors `authenticate(session, key)`
  - 14 new tests: login redirect + 503 unconfigured, callback state-mismatch / unregistered-email-403 / token-exchange-failure / happy-path / `/user/emails` fallback, session cookie authenticates downstream `/v1/dashboard`, `/v1/auth/me` shape + 401 path, logout revokes + idempotent without cookie, bearer-wins ordering, revoked cookie falls through to 401
  - Pending: Google OAuth (structurally identical, separate provider config). Tracked under W8.7 (self-serve signup) so it lands alongside the domain-allowlist feature it depends on
- [~] **W6.5 — Shared dashboard** (multi-user view shipped; alerts detection shipped; notification delivery deferred)
  - `GET /v1/dashboard/team` endpoint reads the `runs` table for the caller's org (filtered to terminal states `done`/`failed` so queued/running rows don't skew rollups). Returns `total_runs` + `total_cost_usd` headline numbers, `by_author` leaderboard sorted by spend desc with email + run-count + tokens, `by_task` task-type breakdown, plus `recent` (default 50, max 500) for the activity feed
  - New schemas in `src/claudestruct/server/schema.py`: `AuthorRollup`, `TaskRollup`, `TeamDashboardResponse`. Tenant-scoped via `Run.org_id == principal.org_id` so an org can never see another org's spend
  - **Cost-regression alerts (this PR)**: `src/claudestruct/server/alerts.py` ships `compute_team_alerts(session, *, org_id, lookback_days=30, recent_window_hours=72, sigma_threshold=2.0)` — folds per-task baseline mean/stddev over the lookback window and flags `done` runs in the recent window whose `cost_usd` is more than `sigma_threshold` σ above their task's baseline. Per-task scoping prevents `cs plan` from firing alerts just because `cs review` is cheap; sample-size floor of 5 suppresses spurious alerts on tasks with too little history. Failed runs and negative costs are excluded from the baseline (they would distort the mean).
  - `GET /v1/alerts` (member+, org-scoped, mirrors `/v1/dashboard/team`'s tenant model) with `lookback_days` / `recent_window_hours` / `sigma_threshold` query knobs. Returns `baselines[]` + `alerts[]`; baselines surface `stddev=null` when below the sample-size floor so the SPA can render "insufficient data" rather than a noisy estimate. Alerts sorted by z_score desc, capped at 200.
  - New schemas: `TaskBaselineResponse`, `RunAlertResponse`, `TeamAlertsResponse`. Router mounted in `app.py` after the existing SLO router.
  - Tests: `tests/test_alerts.py` (23 cases) — `_mean_stddev` math (empty / single / sample-stddev with hand-computed value), `compute_team_alerts` covering empty / below-min-sample / steady-cost / clear-outlier / per-task-isolation / outside-recent-window / failed-run-excluded / negative-cost-excluded / cross-tenant-isolation / outside-lookback / sort order / custom threshold honored; HTTP endpoint covering 401 unauth / 403 viewer / 200 member / response shape / param validation (lookback=0 → 422, sigma=0 → 422) / X-CS-Region header / end-to-end seeded outlier
  - Pending: notification delivery (Slack webhook / email) — detection now provides a stable surface for the deliverer to poll; budget-cap rollups per team (separate alert kind on the same endpoint)
- [~] **W6.6 — GitHub App** (webhook receiver + outbound ack comment + verdict-on-completion + Checks API shipped; bot-as-actor PR opens deferred)
  - `GitHubInstallation` SQLAlchemy model maps `installation_id` ↔ `org_id` with per-install `webhook_secret` + optional `repo_filter` substring + `bot_user_id` sentinel for attribution
  - `POST /v1/github/webhook` — auth-bypassing endpoint (signature is the only gate); reads raw body for HMAC stability before JSON-parsing; verifies `X-Hub-Signature-256` via `hmac.compare_digest`; same 401 status on unknown installation AND bad signature so attackers can't enumerate IDs
  - Trigger detection in `routers/github.py:detect_trigger()` matches `pull_request.opened|synchronize|reopened` and `issue_comment.created` whose body contains `/cs review` (case-insensitive); ignores plain (non-PR) issues; runs are attributed to the install's bot user so the team-dashboard leaderboard shows them as `github-bot@<slug>` rather than mis-crediting a human
  - `cs serve add-github-install <installation_id> <org_slug> [--secret SECRET] [--repo-filter SUB]` CLI subcommand auto-generates the webhook secret if omitted, prints it once, creates the sentinel bot user + membership idempotently
  - **Outbound API (this PR)**: `src/claudestruct/server/github_app.py` ships `mint_app_jwt` (RS256 JWT identifying the App, 9-min lifetime under GitHub's 10-min cap, 30s drift buffer on `iat`), `mint_installation_token` (POST `/app/installations/{id}/access_tokens`, parses `(token, expires_at)`), `InstallationTokenCache` (thread-safe in-process cache with 5-min refresh buffer + per-install isolation + manual `invalidate`), `post_pr_comment` (POST `/repos/{owner}/{name}/issues/{n}/comments`, returns `html_url`), and a top-level `post_ack_comment` that ties the three together. Configuration via `CLAUDESTRUCT_GITHUB_APP_ID` + `CLAUDESTRUCT_GITHUB_APP_PRIVATE_KEY_PEM` env vars; `load_app_config()` returns `None` when unconfigured so deployments without App credentials skip outbound posting silently
  - The webhook handler now posts an acknowledgement comment back to the PR after enqueuing the run ("🤖 claudeStruct queued review run `run-...`"), best-effort: any GitHubAppError or network failure is logged + swallowed so it can't break the webhook response that already 202-acked the run. Test seam: `app.state.github_app_http_client` injection mirrors the OAuth router's pattern
  - Tests: 14 prior webhook cases unchanged + 20 new for outbound: load_app_config (unset / both-set / half-set), JWT mint (3-segment shape, claim fields, RSA-only enforcement, garbage-PEM rejection, signature verifies with public key), install-token mint (parses response, raises on non-201, raises on missing fields), cache (mint-then-reuse, refresh-when-near-expiry, invalidate-forces-refresh, per-install isolation), post_pr_comment (returns html_url, rejects bad repo format, raises on non-201, raises on missing html_url), end-to-end post_ack_comment (full mint→post sequence + cache reuse on second call). Total Python: 323 passed; ruff clean
  - **Verdict-on-completion (this PR)**: `Run` model gains nullable `github_installation_id` / `github_repo_full_name` / `github_pr_number` columns so the worker can post the verdict back to the originating PR. The webhook handler persists the PR coordinates onto the queued Run row; `worker.process_pending_run` calls `_post_verdict_comment_safe(run)` from each terminal branch (done / failed / unknown-task / crash). `format_verdict_body` emits ✅-headlined Markdown for `done` runs (with cost + duration footer) and ❌-headlined Markdown for `failed` runs (with the error truncated to 1500 chars to stay under GitHub's 65k limit and avoid pasting accidentally-captured API keys); unknown statuses fall back to a neutral message. Best-effort: any outbound failure is logged + swallowed so a 503 from GitHub never rolls back the run's terminal state (operators can re-trigger; losing run state would cost real money). Worker shares the install-token cache pattern via a process-wide `InstallationTokenCache` initialized lazily; tests inject a stub via `worker.http_client_factory`
  - Tests: 14 prior webhook cases unchanged + 20 prior outbound + 13 new verdict cases — `format_verdict_body` (done body shape with cost/duration, failed body truncates 5KB error, empty-error fallback, unknown-status neutral message, missing-duration "unknown"), `Run.github_*` column round-trip + null defaults for CLI runs, webhook persists PR coordinates, worker posts on done + on failed runs, worker skips when run has no github_* fields (CLI/REST runs), worker skips when App not configured, worker swallows outbound 503 without rolling back run state. Total Python: 316 passed; ruff clean
  - **Checks API (this PR)**: `Run` gains nullable `github_head_sha` + `github_check_run_id` columns; webhook captures `pull_request.head.sha` (None for issue_comment triggers — those still get the comment-based verdict). `github_app.py` ships `format_check_run_payload` (validates status / conclusion / output combinations so a typo can't reach the network and 422 there), `post_check_run` (POST `/repos/.../check-runs`), `patch_check_run` (PATCH for in_progress → completed transition), `format_completed_check_payload` (done→success, failed→failure with 1500-char truncated error, unknown→neutral), and `post_check_run_with_token` / `patch_check_run_with_token` glue. Worker posts an `in_progress` check-run on claim (persisting the returned id onto the Run row) and PATCHes to `completed` from each terminal branch; if the in_progress POST failed (503) the completion path falls back to a fresh POST so the PR still shows the verdict instead of leaving a stale "in progress". Best-effort throughout — outbound failures are logged + swallowed so a GitHub blip never rolls back the run state.
  - Tests: 27 new in `tests/test_checks_api.py` — `format_check_run_payload` shape + validation (minimal; in_progress with output; completed-without-conclusion rejected; invalid-conclusion rejected; conclusion-without-completed rejected; invalid-status rejected; output title default); `format_completed_check_payload` outcome mapping (done→success, failed→failure, error truncation, unknown→neutral); `post_check_run` HTTP (returns id; bad-repo-format reject; non-201 raises; missing-id raises); `patch_check_run` HTTP (URL contains id; non-200 raises); worker integration (opens in_progress on claim + persists id; skips when no head_sha; falls back to fresh POST when in_progress 503'd; skips when App not configured; swallows total outbound failure without rolling back run state; failed run produces conclusion=failure); detect_trigger head_sha extraction (PR happy, PR missing head, issue_comment); column round-trip. Total Python: 356 passed; ruff clean
  - Pending: opening PRs as `claudeStruct[bot]` (would need a separate "create commit on a new branch" path; W6.6 deferred); annotation-level findings (per-line check-run annotations) — possible follow-up once the verdict has structured findings to attach
- [x] **W6.7 — GitLab + Bitbucket integrations**
  - `src/claudestruct/integrations/gitlab-ci-cs-review.yml` — MR-triggered job, posts verdict via GitLab Notes API using `CI_JOB_TOKEN`. Honors `ANTHROPIC_API_KEY` + optional `CLAUDESTRUCT_MONTHLY_CAP_USD`.
  - `src/claudestruct/integrations/bitbucket-pipelines-cs-review.yml` — `pull-requests."**"` step, posts via Bitbucket 2.0 Comments API using `BITBUCKET_USER` + `BITBUCKET_APP_PASSWORD`.
  - `integrations/README.md` documents the install / env-var setup for both, plus a "common knobs" section covering `--max-bytes` + `CLAUDESTRUCT_MONTHLY_CAP_USD` across all three templates.

---

## Wave 7 — Ecosystem & GTM (Q3b)

Goal: distribution. Make the product discoverable, easy to install, and easy to extend.

- [ ] **W7.1 — VS Code extension**
  - Surfaces `cs review` on the diff in SCM gutter; `cs dev`/`cs debug` in command palette
  - Uses daemon REST API when available, falls back to local CLI
  - Published to VS Marketplace + Open VSX
- [ ] **W7.2 — JetBrains plugin**
  - Same surface for IntelliJ / PyCharm / WebStorm; tool window for run history
  - Published to JetBrains Marketplace
- [~] **W7.3 — Skills marketplace** (claw-squad; registry hosting deferred)
  - `claw-squad/src/skills-registry.ts` — manifest format with `id` / `version` / `url` / `sha256` (+ optional `applyTo`/`license`/`homepage`/`publishedAt`); `parseManifest`, `loadRegistryIndex`, `installSkill`, `uninstallSkill`, `listInstalled`
  - CLI: `claw-squad skills list / install <idOrUrl> / uninstall <id>`. `--registry` overrides `CLAW_SKILLS_REGISTRY` env / default URL. Accepts ids, https manifest URLs, and `file://` air-gapped paths.
  - sha256 verification on install — refuses on mismatch. Sidecar `<id>.md.manifest.json` keeps provenance inspectable.
  - Tests: `claw-squad/tests/skills-registry.test.ts` (18 cases) — manifest validation, registry index shapes (`[]` and `{manifests: [...]}`), file:// + https loaders, install round-trip, sha256-mismatch refusal, install + uninstall + list lifecycle
  - Pending: actual hosting at `skills.claudestruct.dev` (manual infra step) and cosign signature verification (deferred above the sha256 layer)
- [x] **W7.4 — Plugin SDK for new agents** (claw-squad)
  - `claw-squad/src/plugins.ts` — `ClawSquadPlugin` interface with `apiVersion` / `name` / `subagents[]` / `skills[]`. `PLUGIN_API_VERSION` constant lets the host skip incompatible plugins at load time with a warning rather than a crash.
  - Auto-discovery: scan `<repoRoot>/node_modules/claudestruct-plugin-*`, resolve entry via `package.json` `main` / `exports["."]` / fallbacks, dynamic-import via `pathToFileURL`, validate with `isPlugin()`, merge with deterministic dedup (first plugin wins for duplicate names, surfaced as a warning).
  - Plugins contribute new subagents and skills only — Planner/Coder/Reviewer roles stay core (a plugin flipping the orchestrator state machine breaks every other plugin).
  - Tests: `claw-squad/tests/plugins.test.ts` (20 cases) — `isPlugin` validation matrix, prefix discovery + non-dir filtering, CJS + ESM loaders, missing entry / bad shape / wrong apiVersion warnings, merge dedup of plugins/subagents/skills, end-to-end `loadPluginsFromRepo`
  - Pending: PyPI-side equivalent (claudestruct plugins), published `claudestruct-plugin-sdk` package on npm
- [ ] **W7.5 — Public playground**
  - `playground.claudestruct.dev` with read-only sample runs, no key required
  - Limited to a 10k-token-per-day shared bucket; rate-limited per IP
- [ ] **W7.6 — Marketing + docs site upgrade**
  - Landing page, pricing page, demo videos, case studies
  - Algolia DocSearch; analytics via Plausible (privacy-friendly)
- [~] **W7.7 — Distribution channels** (templates only; publishing waits on W4.3)
  - `packaging/homebrew/claudestruct.rb` — formula template with virtualenv install + smoke test
  - `packaging/scoop/claudestruct.json` — Scoop manifest with `checkver` autoupdate hooks
  - `packaging/aur/PKGBUILD` — Arch User Repository recipe with `python -m build` + `python -m installer`
  - `packaging/snap/snapcraft.yaml` — Snapcraft strict-confinement recipe with `home`/`network`/`removable-media` plugs
  - `packaging/README.md` documents the per-channel manual publish flow
  - `docs/install.md` lists the channels with status (`template` until release automation lands)
  - Pending: actual publish to each channel — needs `tonyandclaw/homebrew-tap` + `tonyandclaw/scoop-bucket` repos and AUR + Snapcraft accounts (manual infra)

---

## Wave 8 — Hosted SaaS launch (Q4)

Goal: a managed service teams pay for. Open-core split: Waves 4-7 OSS, Wave 8 hosted layer (closed-source or AGPL with separate hosted offering).

- [~] **W8.1 — Multi-tenant infrastructure** (skeletons; production layering pending)
  - `deploy/helm/claudestruct/` — Chart 0.1.0 with Deployment running `cs serve run`, Service on 8787, optional Ingress, Secret (Anthropic / Stripe / DB URL), `_helpers.tpl`. Probes against `/healthz` + `/readyz`. Strict pod securityContext: non-root, read-only root FS, all caps dropped.
  - `deploy/terraform/main.tf` — VPC across two AZs, RDS Postgres 16 (Multi-AZ when `environment="prod"`), Secrets Manager-managed master password, security group locked to in-VPC traffic. Outputs the DB endpoint + secret ARN for the Helm chart.
  - `deploy/README.md` documents install + roadmap mapping (what each follow-up PR should layer in).
  - Pending: EKS cluster module, ALB+ACM ingress, per-region instantiation for W8.5, HPA, Redis for the W6.1 job queue.
- [~] **W8.2 — Billing & subscription** (skeleton + token-cap enforcement shipped; live Stripe behind `[hosted]` extra)
  - `src/claudestruct/server/billing.py` — `Subscription` model (one row per org, `free`/`team`/`business` tier), `get_or_default()` lazy-materializes a free placeholder, `current_period_bounds()` falls back to the UTC calendar month when Stripe state is absent, `AUDIT_RETENTION_DAYS` map drives W8.4 pruning.
  - Routes: `GET /v1/billing/subscription` (viewer+), `POST /v1/billing/checkout` (admin), `GET /v1/billing/usage` (viewer+), `POST /v1/billing/webhook` (unauthenticated, signature-verified).
  - Stripe SDK lazy-imported via `stripe_sdk_available()`; checkout returns a deterministic stub URL on the OSS path; webhook returns 503 with a clear "install stripe" message. `billing.checkout.create` writes an audit row under the caller's org chain (W8.4 integration).
  - **Token-cap enforcement (this PR)**: `TIER_TOKEN_CAPS` (free=100k, team/business=None) + `tier_token_cap()` lookup with defensive free-fallback for unknown tiers; `current_period_token_usage(session, org_id)` sums `Run.input_tokens + output_tokens` over the org's current billing window (Stripe `current_period_*` if set, else UTC calendar month). `_enforce_token_cap()` in `routers/runs.py` rejects `POST /v1/runs` with **HTTP 402 Payment Required** + structured `TokenCapExceededResponse` body (`detail`, `used_tokens`, `cap_tokens`, `period_end`, `tier`) when an org has met or exceeded its cap. Failed runs still count toward the cap (the Anthropic API call already happened). Implicit-free orgs (no Subscription row) and orgs with unknown tier strings both fall back to free semantics — no path can accidentally bypass the gate.
  - Tests: `tests/test_billing.py` (17 prior + 4 new — `tier_token_cap` table + unknown-tier fallback + `current_period_token_usage` sums incl. failed runs + tenant isolation + window filtering); `tests/test_server.py` (8 new — under-cap 202, at-cap 402 with body shape, team-tier unlimited, business-tier unlimited, failed-runs count, unknown-tier-falls-back-to-free, no-Subscription-row treats as free, cross-org usage doesn't leak). Total Python: 329 passed; ruff clean.
  - Pending: live Checkout integration, canonical Stripe webhook handlers, invoice PDF passthrough.
- [~] **W8.3 — Tenant-scoped sandbox** (per-tier soft limits shipped; per-run Docker container deferred)
  - `SandboxLimits` + `SANDBOX_LIMITS` table in `src/claudestruct/server/billing.py`: free=(1 concurrent, 5min, $0.50), team=(4 concurrent, 15min, $5), business=(16 concurrent, 60min, $50). `sandbox_limits_for_tier(tier_str)` falls back to free on unknown tiers (defense-in-depth so a future tier name can't accidentally grant business ceilings)
  - `TIER_PRIORITY` (business=30 > team=20 > free=10) prevents free-tier bursts from starving paying customers
  - Worker `_claim_one()` in `src/claudestruct/server/worker.py` rewritten: fetches all queued rows, joins per-org subscription, skips rows whose org is at concurrency cap, picks highest-tier row first (FIFO within tier). Eligible-set materialised in Python — fine for SQLite + thousands of queued rows; Postgres path via SELECT-FOR-UPDATE-SKIP-LOCKED tracked for the W8.3 follow-up
  - `GET /v1/billing/subscription` now returns `sandbox_limits: { max_concurrent_runs, max_runtime_seconds, max_cost_usd }` so the SPA / CLI can render quota state without re-implementing the lookup table
  - 6 new tests: free-tier concurrent cap blocks second claim, team-tier 4-of-5 cap, business priority jumps queue past older free run, limits surface in subscription response, unknown-tier falls back to free, per-org concurrency (org-A in flight ≠ org-B in flight)
  - Pending: per-run Docker container (one container per orchestrator-run with CPU/memory quotas via `--cpus`/`--memory`) — needs the W4.4 GHCR image as the runtime base + a Docker socket policy on the daemon host. Tracked separately because the orchestration layer is sizable
- [x] **W8.4 — Append-only hash-chained audit log**
  - `src/claudestruct/server/audit.py` — `AuditEntry` model with per-org `seq` + `prev_hash` + `entry_hash` columns, `compute_entry_hash()` over canonical-JSON payload + identity fields + ISO-8601 created_at, `record()` appends with the chain link computed, `verify_chain()` walks forward and reports the first divergence (`broken_at_seq`, `broken_reason`).
  - `src/claudestruct/server/routers/audit.py` — `GET /v1/audit/head` (viewer+; returns the all-zero genesis sentinel for empty chains), `GET /v1/audit` (admin; paginated, cursor-based), `GET /v1/audit/verify` (admin).
  - Wired into `keys.create / keys.revoke / runs.submit / billing.checkout.create` so every state-changing call appends exactly one row. Webhook receipt records `stripe.<event_type>` once Stripe is configured.
  - `prune_audit()` enforces tier-driven retention (free=90d, paid=7y; pulled from `billing.AUDIT_RETENTION_DAYS`). Pruning is intentionally chain-breaking — operators record the post-prune head externally before running it.
  - Tests: `tests/test_audit.py` (17 cases) — canonical-JSON determinism, per-org isolation, chain happy path, tampered-payload + seq-gap detection, prune semantics, HTTP auth gate + RBAC + pagination, payload round-trip + multi-tenant isolation.
- [x] **W8.5 — Data residency**
  - `resolve_region(override)` in `src/claudestruct/server/app.py` (priority: explicit > `CLAUDESTRUCT_REGION` env > `us-east-1` default); `RegionHeaderMiddleware` stamps every response with `X-CS-Region`; `/healthz` + `/readyz` echo it in their bodies
  - `Subscription.region` column on the billing model so the hosted control plane can pin a tenant to one residency shard
  - Real residency enforcement still happens at the load-balancer / DNS layer (per-region deployments + tenant region pin) — the header is the proof, not the gate
  - Tests: `tests/test_residency.py` (10 cases) — `resolve_region` priority, header on every endpoint (incl. unauthenticated `/healthz` + `/openapi.json`), body fields, DB column round-trip + null default
- [x] **W8.6 — Customer-managed encryption keys (CMEK)**
  - `src/claudestruct/server/crypto.py` — `WrappedDEK` dataclass, `LocalKMSProvider` (KEK from `CLAUDESTRUCT_KEK_PASSPHRASE` via PBKDF2-HMAC-SHA256, random fallback for dev), `fresh_dek` / `encrypt_field` / `decrypt_field` (AES-GCM with AAD-bound row identity), `encode_b64` / `decode_b64` URL-safe helpers, `default_provider()` factory keyed off `CLAUDESTRUCT_KMS_PROVIDER`
  - `Subscription.wrapped_dek_b64` / `wrapped_dek_provider` / `wrapped_dek_key_id` columns store the org-scoped envelope
  - AWS KMS provider path stubbed (`NotImplementedError`) — abstraction is in place, real SDK integration ships when the merchant story needs it
  - Tests: `tests/test_crypto.py` (20 cases) — local round-trip, passphrase isolation, AAD binding, wrong-DEK / truncated-blob / wrong-provider rejection, default-provider env wiring
- [~] **W8.7 — Status page + SLO dashboard** (SLO endpoint shipped; external status page deferred)
  - `src/claudestruct/server/slo.py` folds the `runs` table into rolling 24h / 7d / 30d windows; emits `success_rate`, `error_rate`, p50/p95/p99 of run-start latency (`started_at - created_at`) and duration (`duration_ms` for `done` runs only). Targets (`SUCCESS_RATE_TARGET=0.999`, `P95_RUN_START_MS_TARGET=5000`, `P95_DURATION_MS_TARGET=600000`) live as code-reviewed constants
  - `GET /v1/slo` is unauthenticated like `/healthz` so an external status page can scrape without a service token; output is aggregate (no run IDs / payloads / per-tenant data)
  - Failed runs excluded from duration percentiles (a single crash shouldn't poison p95); negative run-start deltas clamp to 0 (clock-skew defense); `null` percentiles surface for empty windows so consumers render "n/a" not 0
  - `docs/slo.md` documents the targets, the response shape, what's measured (and what isn't), and the status-page traffic-light mapping
  - Tests: `tests/test_slo.py` (20 cases) — `_percentile` math (empty / single / odd-count / p0 / p100 / p95 linear interp), windowing (24h excludes 25h-old, 30d excludes 31d-old, queued/running excluded), success/error rate, run-start latency clamps + skips runs with `started_at=NULL`, duration excludes failed runs, endpoint shape + region header
  - Pending: external `status.claudestruct.dev` (manual infra), per-tenant SLO endpoint, API request-latency metric (needs FastAPI middleware), public incident timeline

---

## Optional Wave 9 — Enterprise top-up (deferred until Wave 8 lands enterprise contracts)

- SAML 2.0 SSO (Okta, Azure AD)
- SCIM 2.0 user provisioning
- Tenant-isolated VPC peering
- SOC2 Type II prep (gap analysis once Wave 5+8 controls are in place)
- HIPAA-eligible deployment
- Air-gapped self-hosted edition (Helm chart for fully-isolated installs)

Reopen criterion: a signed enterprise contract or three serious leads asking for any item above.

---

## Post-roadmap PRs (selected from R/F candidate list)

- [~] **R2 + F1 + F8 — orchestrator integration test, MCP server, cross-tool dashboard**
  - **R2** orchestrator integration test: `claw-squad/tests/orchestrator-integration.test.ts` covers Phase 1 → Phase 3 with scripted MockProvider per role on a real tmp git repo. Three scenarios: happy path (`reason=complete`), maxReviewRounds rollback (`rolledBack=true` + branch reverted), maxCostUsd abort (`reason=aborted` + snapshot persisted). Unblocks future orchestrator refactors.
  - **F1** MCP server (`cs mcp`): exposes the four task modes plus dashboard + metrics as MCP tools so Claude Code can call claudestruct directly. New `mcp_handlers.py` (pure dict-in/dict-out), `mcp_server.py` (stdio bootstrap with lazy SDK import), `pyproject.toml` adds `mcp>=1.0.0`. `CLAUDE.md` documents the `.mcp.json` config snippet.
  - **F1 prep**: extracted `run_task_and_log` from `cli._run_common` into `runner.py` so CLI + MCP share one business-logic path. Pure refactor, behavior unchanged.
  - **F8** cross-tool dashboard (`cs dashboard --include-claw-squad`): folds `.claw-squad/runs/*.jsonl` into the same `RunSummary` table with a `tool` column. Schema parity from W2.1 made the mapping cheap.

### Verification
| Suite | Result |
|---|---|
| `pytest tests/` (Python) | 78 passed (+16 new: 9 MCP handlers + 7 cross-tool dashboard) |
| `npx vitest run` (claw-squad) | 257 passed (25 files; +3 new orchestrator-integration scenarios) |
| `npx tsc --noEmit` | clean |

---

## Last Update

- 2026-05-05 — W6.5 advance: cost-regression alerts detection shipped:
  - `src/claudestruct/server/alerts.py` — per-task baseline (mean / sample-stddev) over the org's lookback window; flags `done` runs in the recent eval window whose cost exceeds `mean + sigma_threshold * stddev`. Failed runs and negative costs excluded from the baseline; sample-size floor of 5 suppresses noisy estimates
  - `GET /v1/alerts` (member+, org-scoped) with `lookback_days` / `recent_window_hours` / `sigma_threshold` query knobs
  - 23 new tests covering math, baseline / alert flagging, per-task scoping, tenant isolation, recent-window filter, RBAC + HTTP shape. Total Python: 379 passed, 3 skipped; ruff clean
  - Pending under W6.5: Slack/email notification delivery (the endpoint is what those deliverers will poll), per-team budget-cap rollups
- 2026-04-28 — W6.6 close-out: GitHub Checks API integration shipped:
  - `Run` gains nullable `github_head_sha` + `github_check_run_id` columns; webhook persists `pull_request.head.sha` (None for issue_comment triggers — comment-based verdict still fires)
  - `github_app.py` adds `format_check_run_payload` (with status/conclusion validation), `post_check_run`, `patch_check_run`, `format_completed_check_payload`, plus the `_with_token` glue mirroring `post_ack_comment`
  - Worker opens an `in_progress` check-run on claim (persisting id), PATCHes to `completed` on terminal; falls back to a fresh POST if the open failed so the PR never shows a stale "in progress". Best-effort throughout
  - 27 new tests cover format validation, HTTP behaviour, completion mapping, and worker integration (success / fallback / skip / swallow / failed-run paths). Total Python: 356 passed; ruff clean
  - Pending under W6.6: claudeStruct[bot] PR-opens (needs branch-create path); per-line annotations
- 2026-04-28 — W8.2 advance: per-tier token-cap enforcement at run-submit time:
  - `TIER_TOKEN_CAPS` (free=100k/month, team/business=uncapped) + `tier_token_cap()` defensive free-fallback for unknown tiers
  - `current_period_token_usage(session, org_id)` sums input+output tokens over the org's current billing window via existing `current_period_bounds()`
  - `POST /v1/runs` now returns **HTTP 402 Payment Required** with a structured `TokenCapExceededResponse` body (`used_tokens`, `cap_tokens`, `period_end`, `tier`) when an org has met or exceeded its cap. Failed runs still count toward the cap (the Anthropic API call happened); orgs with no Subscription row treat as free; unknown tier strings fall back to free
  - 12 new tests (4 in test_billing.py + 8 in test_server.py). Total Python: 329 passed; ruff clean
  - Pending under W8.2: live Stripe checkout, webhook handlers, invoice PDF passthrough
- 2026-04-28 — W6.6 close-out (mostly): verdict-on-completion comment shipped:
  - `Run` gains nullable `github_installation_id` / `github_repo_full_name` / `github_pr_number` columns; webhook persists them when enqueuing
  - `format_verdict_body` emits ✅ for done (cost + duration) / ❌ for failed (1500-char truncated error block) / neutral for unknown statuses
  - `worker.process_pending_run` calls `_post_verdict_comment_safe(run)` from every terminal branch (done / failed / unknown-task / crash); best-effort, swallows outbound failures so a GitHub 503 can't roll back the run state
  - 13 new tests (5 format_verdict_body shapes + 2 Run column round-trip + 1 webhook persistence + 5 worker integration: done, failed, no-github-fields, no-app-config, swallows-outbound-failure). Total Python: 316 passed; ruff clean
  - Pending: GitHub Checks API integration; opening PRs as `claudeStruct[bot]`
- 2026-04-28 — W6.6 advance: GitHub App outbound + ack comment shipped:
  - `github_app.py` ships `mint_app_jwt` (RS256 JWT under GitHub's 10-min cap), `mint_installation_token` + `InstallationTokenCache` (thread-safe in-process cache with 5-min refresh buffer), `post_pr_comment` + `post_ack_comment` end-to-end glue
  - Webhook handler posts an acknowledgement comment back to the PR after enqueuing the run; best-effort, never blocks the webhook 202 response
  - 20 new tests covering JWT signing (incl. RSA-only enforcement + public-key verification round-trip), install-token mint, cache lifecycle, comment post, and end-to-end ack flow. Total Python: 323 passed; ruff clean
  - Pending: verdict-on-completion comment (needs worker hook), Checks API integration
- 2026-04-28 — W8.7 SLO endpoint ready for PR push (PR #33 incoming):
  - `src/claudestruct/server/slo.py` rolls the `runs` table into 24h / 7d / 30d windows: `success_rate`, p50/p95/p99 of run-start latency + duration. Targets are code-reviewed constants (`0.999` success, 5s p95 run-start, 10min p95 duration)
  - `GET /v1/slo` is unauthenticated (status-page friendly); response is aggregate-only so leaving it open trades nothing sensitive
  - `docs/slo.md` documents targets, response shape, traffic-light status-page mapping, and the explicit out-of-scope list (external `status.claudestruct.dev`, per-tenant SLO, API request-latency middleware)
  - 20 new tests covering percentile math, windowing, success/error rate, run-start clock-skew clamping, failed-run exclusion from duration percentiles, endpoint shape + region header. Total Python: 303 passed; ruff clean
- 2026-04-28 — W8.5 + W8.6 merged via PR #32:
  - W8.5: `resolve_region` priority chain + `RegionHeaderMiddleware` (`X-CS-Region` on every response) + `Subscription.region` column. 10 tests
  - W8.6: `LocalKMSProvider` (passphrase-derived KEK, AES-GCM field-level encrypt/decrypt with AAD), `WrappedDEK` columns on `Subscription`. 20 tests. AWS path stubbed pending merchant integration
- 2026-04-27 — W8.3 tenant-scoped sandbox (limits + priority) ready for PR push:
  - `SANDBOX_LIMITS` + `TIER_PRIORITY` per-tier tables in billing module; worker `_claim_one()` skips orgs at their concurrency cap and picks highest tier first; `/v1/billing/subscription` surfaces the active limits
  - 6 new tests + 1 augment to existing billing test. Total Python: 234 passed (audit pre-existing failures unchanged); ruff clean
  - Per-run Docker container with CPU/memory quotas deferred — separate orchestration-layer PR
- 2026-04-27 — W6.4 GitHub OAuth login ready for PR push:
  - `UserSession` model + `oauth.py` provider helpers + `routers/oauth.py` with login / callback / me / logout
  - `auth.current_principal` now accepts a session cookie as fallback when no bearer is present (bearer still wins on conflict)
  - 14 new tests: redirect / 503 / state mismatch / unregistered-email-403 / token-exchange-failure / happy-path / `/user/emails` fallback / cookie auth on dashboard / `/me` / logout idempotent / bearer-precedence / revoked-cookie. Total Python: 228 passed (2 pre-existing audit failures untouched)
  - Google OAuth deferred — structurally identical, lands with self-serve signup work
- 2026-04-27 — W6.6 GitHub webhook receiver ready for PR push:
  - `POST /v1/github/webhook` with HMAC SHA-256 signature verification, installation→org mapping, trigger detection for PR open/sync/reopen + `/cs review` comments, repo substring filter, sentinel bot user attribution
  - `cs serve add-github-install` CLI for registration
  - 14 new tests (signature/install gate, every trigger surface, cross-org isolation). Total Python: 182 passed; ruff clean
  - Outbound API (PR comments, Checks API, bot-as-actor) deferred — needs GitHub App private key + token minting, separate PR
- 2026-04-26 — W5.3 close-out: `claw-squad runs purge` shipped. New `src/runs/purge.ts` mirrors `claudestruct.redact.purge_runs` (mtime-based, `dryRun`, injectable `now`); `claw-squad runs list` lists run logs with age/size; `claw-squad runs purge --older-than-days N [--dry-run]` prunes them. 7 new vitest cases in `tests/runs-purge.test.ts`. Total TS: 309 tests passing; tsc clean. Removes the only "Pending" tail on W5.3.
- 2026-04-26 — Wave 6 mid-roll ready for PR push:
  - W6.1 ✅ daemon-mode background runner: `Run` model, `process_pending_run` + `WorkerThread`, `drain_queue`, `cs serve worker` subcommand, real DB-backed POST/GET runs with tenant isolation (cross-org → 404)
  - W6.5 🟢 shared dashboard: `GET /v1/dashboard/team` with author leaderboard + task breakdown + recent feed; alerts/regression detection deferred until a notification surface exists
  - 16 new server tests (worker drain + failure recovery + queue empty + isolation; team dashboard 5 cases including cross-org isolation, queued-row exclusion, limit validation). Total Python: 168 passed
- 2026-04-26 — Wave 5 close-out ready for PR push:
  - W5.1 ✅ claw-squad TS-side tracing landed (root `clawSquad.run` span; `optionalDependencies` block for OTel deps; 7 new vitest cases). Wave 5 OTel item now fully done across both tools.
  - W5.5 ✅ Hardened sandbox: seccomp.json + apparmor.profile + sandbox-hardening.md docs + Linux uid-map probe so the structured isolation report reflects actual netns capability.
  - Stats: 139 Python tests + 302 TS tests + Go isolation-report tests; type-check clean.
- 2026-04-26 — Wave 4 close-out + Wave 5 kick-off ready for PR push:
  - W4.2 ✅ CODE_OF_CONDUCT.md (official Contributor Covenant 2.1, contact pointer to SECURITY.md)
  - W4.3 ~ `.github/workflows/release.yml` written (OIDC PyPI + npm provenance + cross-compiled `claw-sandbox` binaries via 4-job matrix); trusted-publishing config still org-level manual step
  - W5.1 ~ `tracing.py` shipped for claudestruct with 6 tests; claw-squad TS-side tracing remains pending
  - W5.2 ✅ `sentry_init.py` shipped with 10 tests covering every redaction surface
  - 16 new Python tests; full pytest 94 passed
- 2026-04-26 — R2 + F1 + F8 PR open at [#19](https://github.com/tonyandclaw/claudeStruct/pull/19) → merged.
- 2026-04-26 — Wave 4 (W4.1, W4.4, W4.5) merged via [#17](https://github.com/tonyandclaw/claudeStruct/pull/17).
- 2026-04-25 — Waves 1-3 closed (PRs #11, #13, #14, #15, #16 merged). Wave 4-8 commercialization roadmap added.
