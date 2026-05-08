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
🆕 **Wave 9** (opt-in) — local-inference / GX10 ready

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
- [x] **W4.3 — Release automation**
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
- [~] **W4.6 — README polish + demo** (most polish shipped; live demo gated on external API key)
  - CI / Docs / License / Container badges added to the README header
  - README rewrite covers the full Wave 9/10 surface: `cs serve` / `cs mcp` / `cs index` + `--smart-context` / `cs voice` / `cs dataset export` / `cs dashboard / metrics` / `claw-squad` / `claw-sandbox`. Provider matrix (cloud Anthropic / cloud OpenAI / Ollama / vLLM / SGLang / llama.cpp) called out
  - Install section enumerates every extra (`[server]` / `[smart-context]` / `[voice]` / `[openai]` / `[otel]` / `[sentry]`) with cost-of-each rationale
  - New "Docs" section indexes `docs/*.md` + `claw-squad/docs/*.md` + `TODO.md` so first-time visitors don't hunt for the deeper material
  - CHANGELOG.md updated to capture the full Wave 10 wave (W10.1–10.10) + W9.4 prompt cache, organised under a Wave 10 sub-header
  - Asciinema recording of `cs dev` / `claw-squad run` — pending (needs an env with a real API key, which CI doesn't have)
  - Per-platform install (Homebrew, scoop, snap, AUR) ships under W7.7 and is now linked from the README

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
- [x] **W5.7 — Test rigor**
  - Ruff lint config in `pyproject.toml` (select F/E/W/I/B/UP/SIM, opinionated rules silenced); CI runs `ruff check src/ tests/` before pytest
  - Coverage gate via `coverage` (`fail_under = 70`; current run hits 80% with CLI/MCP entry points excluded as integration-tested)
  - CI workflow installs `pytest coverage ruff` and runs lint → coverage-gated pytest
  - End-to-end tests with mocked Anthropic SDK shipped: `tests/test_e2e_cli.py` (10 cases) covers the four task commands' happy paths + cost summary + cached-badge surface + provider-side `ClaudestructError` exit-code behaviour + `--log-json` event contract (`run.start` / `agent.usage` / `run.end`) + `--effort xhigh` flag plumbing through to the provider. An autouse fixture stubs `count_tokens` + sets a dummy API key so no test reaches Anthropic.
  - Pending: mutation testing (mutmut / stryker) — heavy CI cost, deferred until a scheduled / nightly job slot is set up

---

## Wave 6 — Team collaboration (Q3a)

Goal: 5-50 devs share the tool with shared visibility, shared budgets, and team-level admin. Today the orchestrator and dashboard are single-user-on-this-machine.

- [x] **W6.1 — Daemon mode** (claudestruct + claw-squad both shipped)
  - `Run` SQLAlchemy model added to `src/claudestruct/server/models.py` with `RunStatus` enum (queued → running → done|failed). Per-row payload (task, description, model, effort, paths_json) plus outcomes (cost_usd, tokens, duration_ms, error)
  - `src/claudestruct/server/worker.py` — `process_pending_run(session, run_root, runner=...)` synchronous drain helper, `WorkerThread` long-running daemon thread with poll-interval + graceful stop, `drain_queue(...)` for cron / batch operation, `wait_until_empty(...)` test helper
  - `POST /v1/runs` now writes a queued `Run` row tagged with `org_id`/`user_id` instead of returning a placeholder id; `GET /v1/runs/{id}` reads from the DB first (with tenant isolation: cross-org lookup → 404, not 403, to avoid leaking existence) and falls back to the legacy JSONL log so pre-W6.1 history stays accessible
  - New `cs serve worker [--once] [--poll-interval N]` CLI subcommand (foreground daemon thread + Ctrl-C graceful drain, or single drain pass)
  - **claw-squad worker daemon (this PR)**: `claw-squad/src/queue.ts` — Redis-backed job queue with `enqueueJob` / `dequeueJob` (atomic rPopLPush) / `completeJob` / `abandonJob` (retry-or-dead-letter) / `requeueDeadJobs` / `queueDepth` / worker heartbeat (`registerWorker` / `heartbeatWorker` / `unregisterWorker` / `listActiveWorkers`). `claw-squad/src/daemon.ts` — `runWorkerDaemon` (long-running, graceful SIGTERM/SIGINT shutdown, 15s heartbeat) + `drainQueueOnce` (cron/batch mode) + `submitJob` (enqueue helper). `claw-squad/src/cli.ts` — `claw-squad worker start` / `once` / `submit` / `status` / `requeue-dead` subcommands. 14 tests in `tests/queue.test.ts`; 445 total pass; tsc clean.
- [x] **W6.2 — HTTP REST API** (session cookie hardening shipped)
  - FastAPI app under `src/claudestruct/server/` behind the `[server]` extra: `/healthz`, `/readyz`, `/v1/dashboard`, `/v1/budget`, `/v1/runs` (POST + GET), `/v1/keys` (list/create/revoke). OpenAPI 3.1 at `/openapi.json`, interactive viewer at `/docs`.
  - Auth: bearer API keys (`ck_<key_id>_<secret>`, SHA-256-hashed secret, last_used stamp on auth success).
  - `POST /v1/runs` returns 202 with a placeholder run_id — actual worker model landed in **W6.1**.
  - **Session cookie hardening**: explicit `HttpOnly` + `Secure` + `SameSite=Lax` attributes verified in tests; expired session rejection; unknown cookie → 401.
  - Tests: `tests/test_server.py` (18 cases) + `tests/test_session_cookies.py` (7 new cases). Total: 25 passed; ruff clean.
  - Pending: per-language SDK stubs (deferred until the API surface is closer to final)
- [x] **W6.3 — User / team / org model + RBAC** (Alembic + teams both shipped)
  - SQLAlchemy 2.x models: `orgs`, `users`, `memberships`, `api_keys`, `teams`, `team_memberships`. Idempotent `init_db()` via `Base.metadata.create_all` for bootstrap; Alembic for ongoing migrations.
  - Roles: `admin` / `member` / `viewer` enforced by `require_role(min_role)` FastAPI dependency
  - `cs serve init-db / add-org / add-user / add-key / add-team / add-team-member / migrate` covers the bootstrap + migration path
  - **Alembic setup**: `alembic.ini` + `alembic/env.py` (reads `DATABASE_URL`, imports all models, `disable_existing_loggers=False` so the migration call doesn't silence loggers in the embedding process) + `cs serve migrate [--revision head|base]` command.
  - **Teams**: `Team` + `TeamMembership` SQLAlchemy models with `(org_id, slug)` and `(team_id, user_id)` UNIQUE constraints. Schema chose separate `team_memberships` table over a `team_id` column on `Membership` so a polyglot engineer can be on `platform-eng` AND `growth` without duplicating their org membership row. `Org.teams` ORM-level cascade ensures dropping an org sweeps up teams + memberships in one step (SQLite can't enforce `ondelete=CASCADE` without the FK pragma; ORM cascade gets us deterministic behaviour either way). CLI subcommands `cs serve add-team <slug> <name> <org>` and `cs serve add-team-member <email> <team> <org>`; both idempotent on re-run; the latter rejects users not already in the org. 14 tests in `tests/test_teams.py`.
  - **Baseline migration**: `alembic/versions/0244691fc128_baseline_schema.py` autogen'd from the live ORM metadata. Smoke test `test_baseline_migration_creates_all_model_tables` runs `alembic upgrade head` against a fresh tmp_path SQLite DB and asserts every `Base.metadata.tables` table is present — catches the case where someone adds a model without regenerating the migration. Also fixed `alembic/script.py.mako` (was using `${rev}` instead of `${up_revision}`, breaking `alembic revision --autogenerate`). 7 tests in `tests/test_alembic.py`.
  - **Team dashboard filter (this PR)**: `GET /v1/dashboard/team?team=<slug>` scopes the rollup to a single intra-org team. Inner JOIN through `team_memberships` materialised in Python (set of `user_id`s) before filtering `runs`. Tenant isolation: a slug that exists in another org returns 404 (not 403) so the endpoint can't be used to enumerate team slugs across tenants. Empty-team short-circuit returns a zero rollup rather than running an `IN ()` query that some DBs reject. 4 tests in `tests/test_server.py`.
- [x] **W6.4 — OAuth login** (GitHub + Google both shipped)
  - `UserSession` SQLAlchemy model: per-row `session_token` (URL-safe random), `provider`, `expires_at` (14d hard cap), `revoked_at` for logout
  - `src/claudestruct/server/oauth.py` — GitHub helpers (`load_github_config`, `build_github_authorize_url`, `exchange_code_for_github_token`, `fetch_github_user`) + Google helpers (`GoogleOAuthConfig`, `load_google_config`, `build_google_authorize_url`, `exchange_code_for_google_token`, `fetch_google_user`). Injectable `http_client` so tests don't hit the providers.
  - `routers/oauth.py`: GitHub (`/v1/auth/github/login`, `/v1/auth/github/callback`) + Google (`/v1/auth/google/login`, `/v1/auth/google/callback`) — CSRF state cookie + redirect, state verify, token exchange, user fetch, session mint, HTTPOnly+Lax+Secure cookie; `/v1/auth/me` (cookie-driven principal); `POST /v1/auth/logout` (revoke + clear). 503 when env vars unset; 403 (not auto-provision) on unknown email.
  - `auth.py:current_principal` chain: bearer wins, then session-cookie fallback. Works for any `provider` value ("github", "google", ...).
  - **Google OAuth (this PR)**: same pattern as GitHub; env vars `CLAUDESTRUCT_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` / `_REDIRECT_BASE`. 13 new tests in `tests/test_google_oauth.py`; 26 total OAuth tests pass; ruff clean.
  - `docs/server.md` updated with OAuth (GitHub + Google) documentation.
- [x] **W6.5 — Shared dashboard** ✅ (multi-user view + cost-regression alerts + budget-cap team rollups all shipped)
  - Multi-user view: `/v1/dashboard/team` — org-scoped rollup by author + task + recent runs (W6.5 part 1)
  - Cost-regression alerts: `cache_state.json` warn-when-low-hit-rate landed earlier
  - **Budget-cap team rollups**: `/v1/budget/team` (viewer+) returns `{org_id, org_slug, tier, period_start, period_end, tokens_used, tokens_cap, cost_used_usd, cost_cap_usd, percent_used, near_limit, exceeded}`. Pulls the period boundary from `current_period_bounds` (Stripe window when set, UTC calendar month otherwise), aggregates `Run.cost_usd` (done + failed only — queued/running excluded so a stale cost estimate can't poison the rollup) via new `current_period_cost(session, org_id)` mirror of `current_period_token_usage`. New `TIER_USD_CAPS` (free=$10/mo, team/business uncapped) + `tier_usd_cap()` lookup with the same defensive fallback as `tier_token_cap`. `percent_used` is the max of (tokens / cap) and (cost / cap); 0 when truly uncapped. `near_limit` (≥80 % and <100 %) is mutually exclusive with `exceeded` (≥100 %) so frontends paint amber vs. red without double-classifying. 12 tests in `tests/test_team_budget.py`: tier-cap matrix, current_period_cost (sum / status filter / empty-org), 401 unauth, free-tier defaults, aggregation across multiple runs, 80 % warn band, exceeded path, uncapped-tier behaviour, cross-org tenant isolation
  - `GET /v1/dashboard/team` endpoint reads the `runs` table for the caller's org (filtered to terminal states `done`/`failed` so queued/running rows don't skew rollups). Returns `total_runs` + `total_cost_usd` headline numbers, `by_author` leaderboard sorted by spend desc with email + run-count + tokens, `by_task` task-type breakdown, plus `recent` (default 50, max 500) for the activity feed
  - New schemas in `src/claudestruct/server/schema.py`: `AuthorRollup`, `TaskRollup`, `TeamDashboardResponse`. Tenant-scoped via `Run.org_id == principal.org_id` so an org can never see another org's spend
  - **Notification surface (this PR)**: `src/claudestruct/server/notify.py` ships a `Notifier` Protocol with two providers — `LogNotifier` (default; structured JSON to stdlib logging at WARNING/ERROR) and `SlackWebhookNotifier` (POSTs an incoming-webhook payload, swallows non-200 so a delivery failure can't crash the producer). Selected via `CLAUDESTRUCT_NOTIFY_PROVIDER=log|slack` (+ `CLAUDESTRUCT_SLACK_WEBHOOK_URL` for slack). Future `EmailNotifier` slots in via the same Protocol
  - **Cost-regression detector (this PR)**: `src/claudestruct/server/alerts.py` folds the `runs` table per org and emits findings for runs whose `cost_usd` is more than `--sigma` (default 2.0) population stddev above the org's mean over `--lookback-days` (default 30). Severity ladder: ≥4σ critical, ≥3σ warning, else info. Excludes failed runs from baseline + candidates (partial work is not comparable). Skips orgs with <3 successful runs (no statistical baseline). Stddev=0 → any positive deviation reports as `inf` σ, severity critical. Findings sort by σ descending so operators see the worst regression first
  - **CLI**: `cs serve alerts [--sigma 2.0] [--lookback-days 30] [--check-recent-hours 24]` runs the detector once and dispatches via the configured notifier — designed for cron / Kubernetes CronJob
  - 25 new tests covering: stddev math (empty, single, population formula); detector behaviour (insufficient baseline skip, zero-stddev skip, below-mean not flagged, above-threshold flagged, old-spike-not-re-alerted, failed-runs excluded, per-org isolation, sort order); severity ladder (info/warning/critical/inf); dispatch round-trip via `_CapturingNotifier`; LogNotifier WARNING vs ERROR levels; SlackWebhookNotifier (empty URL rejected, payload shape, non-200 swallowed); `default_notifier` factory env-driven selection. Total Python: 381 passed; ruff clean
  - **Email notifier**: third concrete `Notifier` impl alongside `LogNotifier` and `SlackWebhookNotifier`. Stdlib `smtplib` (no extra dep), STARTTLS-required (we deliberately don't fall back to plaintext — alert bodies routinely name orgs and quote details). Env-driven config via `CLAUDESTRUCT_NOTIFY_PROVIDER=email` + `CLAUDESTRUCT_EMAIL_SMTP_HOST/PORT/USERNAME/PASSWORD`, `_FROM`, `_TO` (comma-separated). `smtp_factory` injection seam keeps the test suite offline.
  - **Long-running scheduler (this PR)**: `AlertsScheduler` daemon thread in `alerts.py` mirrors the `WorkerThread` pattern from `worker.py` — `threading.Event`-driven loop runs a pass every `interval_s`, `stop()` wakes the sleep so SIGINT/SIGTERM lands promptly without hard-killing an in-flight notifier call. Per-pass exceptions are swallowed and logged so a transient DB blip / bad notifier reply can't take the daemon down. New `cs serve alerts --watch [--interval N]` mode (default `3600s`); without `--watch` the command stays one-shot for cron/CronJob compatibility. Extracted `run_alert_pass()` helper as the shared compute → dispatch path so cron-mode and daemon-mode can't drift. 6 new tests in `tests/test_alerts.py` covering scheduler lifecycle: positive-interval validator, single `run_once` dispatch, exception-swallowing path, full thread start/stop with a 0.05s interval, double-start idempotence, and the shared helper.
  - Pending: budget-cap rollups per team (a "team has burned 80% of monthly cap" alert kind that uses the same notifier surface — small follow-up)
- [x] **W6.6 — GitHub App** (webhook receiver + outbound ack comment + verdict-on-completion + Checks API + branch + PR-open + per-line annotations all shipped)
  - Bot-as-actor PR-open API building blocks landed in `server/github_app.py`:
    - `get_default_branch(repo, install_token, http)` — reads `default_branch` off the repo metadata endpoint so callers don't need to know which key in the payload to read.
    - `get_ref_sha(repo, ref, install_token, http)` — resolves a branch (or any git ref) to its 40-char tip SHA. Accepts both `main` and `refs/heads/main`; normalises internally so the caller doesn't have to remember.
    - `create_branch(repo, branch, base_sha, install_token, http)` — Git Refs API; creates a new branch ref pointed at `base_sha`. Forces `refs/heads/<name>` form in the body even if the caller passed bare; double-prefix-safe.
    - `create_pull_request(repo, head, base, title, body, install_token, http, draft=True)` — Pulls API; defaults to draft so the App's PRs don't immediately page reviewers (a follow-up workflow flips them ready when CI is green).
  - `_StubHttpClient` test fixture extended to record `(verb, url, kw)` triples + a `responses=[…]` queue so multi-step tests (resolve branch → create new branch → open PR) can drive scripted responses.
  - 13 new vitest cases cover happy paths + bad inputs + 4xx error wrapping + the defensive "missing fields" branches that surface a server-side schema drift before it becomes a wrong-data bug.
  - **Worker-side PR-open integration (this PR)**: `open_pr_as_bot` in `github_app.py` ties together `get_default_branch` + `get_ref_sha` + git-over-https clone/push + `create_pull_request`. Worker calls `_open_pr_as_bot_safe(run, run_root)` from the `done` terminal branch. Uses temp dir clone + `git add -A && git commit && git push` via `x-access-token:{install_token}@github.com` URL with credential helper store. Branch name `claudestruct/run-{run_id}` ensures retries don't collide. PR opens as draft; operator marks ready when CI passes. `github_base_branch` already captured at webhook time.
  - **Per-line annotations (this PR)**: `CheckAnnotation` dataclass + `format_annotations_payload` + `annotations` kwarg threaded through `format_check_run_payload` and `format_completed_check_payload`. Annotations surface in the PR's Checks tab as inline comments on specific file+line ranges with `notice`/`warning`/`error` severity levels. `format_check_run_payload` output block now carries `annotations[]` alongside `title`/`summary`.
  - 7 new Python test cases: `CheckAnnotation` dataclass, `format_annotations_payload` (empty/None, single, with-title, empty-list), `format_check_run_payload` with annotations, `format_completed_check_payload` with annotations. Total Python: 361 passed; ruff clean.
  - 14 prior webhook + 20 outbound + 13 verdict + 27 Checks API + 7 new annotation cases = 81 cases; all passing
- [x] **W6.7 — GitLab + Bitbucket integrations**
  - `src/claudestruct/integrations/gitlab-ci-cs-review.yml` — MR-triggered job, posts verdict via GitLab Notes API using `CI_JOB_TOKEN`. Honors `ANTHROPIC_API_KEY` + optional `CLAUDESTRUCT_MONTHLY_CAP_USD`.
  - `src/claudestruct/integrations/bitbucket-pipelines-cs-review.yml` — `pull-requests."**"` step, posts via Bitbucket 2.0 Comments API using `BITBUCKET_USER` + `BITBUCKET_APP_PASSWORD`.
  - `integrations/README.md` documents the install / env-var setup for both, plus a "common knobs" section covering `--max-bytes` + `CLAUDESTRUCT_MONTHLY_CAP_USD` across all three templates.

---

## Wave 7 — Ecosystem & GTM (Q3b)

Goal: distribution. Make the product discoverable, easy to install, and easy to extend.

- [~] **W7.1 — VS Code extension** (sideload-only scaffold shipped; marketplace publish + SCM gutter deferred)
  - `vscode-extension/` standalone TypeScript project with VS Code extension manifest. Not coupled to claw-squad's pnpm workspace — separate publishing target with its own dep tree
  - Five commands: `claudeStruct: Review` / `Dev` / `Plan` / `Debug` / `Dashboard`. Review is one-click from explorer/editor context menu (palette, right-click on file, multi-select). Dev/Plan/Debug prompt for a description via `showInputBox`
  - Output streams to a single `claudeStruct` Output Channel — no WebView (3x activation cost for marginal value over Rich's terminal output)
  - Configuration surface (`claudestruct.cliPath`, `claudestruct.maxBytes`, `claudestruct.effort`, `claudestruct.extraArgs`) lets users point at venv-installed `cs`, override per-task budgets, append `--monthly-cap-usd` / `--log-json` flags
  - Spawn / arg-build logic split into `runner.ts` so vitest can drive it without the `vscode` API. 13 cases cover every config-knob path (default review prompt, required-description guard, `--max-bytes` / `--effort` / `extraArgs` ordering, multi-path forwarding, full composition order, runCs streaming/spawn-error/non-zero exit)
  - Sideload via `vsce package` + `code --install-extension` (documented in `vscode-extension/README.md`)
  - Pending: SCM gutter integration (lights diff lines under `cs review`), daemon REST API client (falls back to local CLI), VS Marketplace + Open VSX publish (needs publisher account + signing)
- [~] **W7.2 — JetBrains plugin** (sideload-only scaffold shipped; marketplace publish + IntelliJ Platform `TestFramework` integration deferred)
  - `jetbrains-plugin/` — Kotlin plugin scaffold mirroring the W7.1 VS Code extension's surface. Same five actions (review / dev / plan / debug / dashboard), same project-scoped settings (`cliPath` / `effort` / `maxBytes` / `extraArgs`), same arg-vector contract — operators switching IDEs reuse the same mental model.
  - `build.gradle.kts` uses IntelliJ Platform Gradle Plugin 2.1, targets 2024.2 platform line (covers IDEA / PyCharm / WebStorm / GoLand / RubyMine / Android Studio); `untilBuild = null` so the plugin keeps loading on newer platform versions until something genuinely breaks (hard upper bound is hostile to operators that pin claudeStruct).
  - Five action classes (`Review`/`Dev`/`Plan`/`Debug`/`Dashboard`Action) plus shared `CsActionBase` that handles spawn + console-tab streaming + path resolution. ANSI colour preserved via `FORCE_COLOR=1` so `cs`'s Rich output reads naturally in the JetBrains console.
  - `CsRunner` (pure Kotlin object) + `CsSettingsState` (project-scoped persistent component) + `CsSettingsConfigurable` (Settings > Tools > claudeStruct panel).
  - 7 unit tests in `CsRunnerTest` covering arg-vector building (review with no description, dev with paths, effort/max-bytes plumbing, extraArgs forward order, dashboard, sentinel handling for `maxBytes=0` / blank effort).
  - `jetbrains-plugin/README.md` covers build (`gradle buildPlugin`), sideload install (Settings > Plugins > Install from Disk), settings panel walkthrough, why a separate IDE plugin makes sense.
  - Build verification deferred — Gradle build pulls the IntelliJ Platform SDK from `download.jetbrains.com`, which the dev sandbox can't reach (403 from the proxy). Operator with internet access runs `gradle buildPlugin` to produce the install ZIP.
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
- [~] **W7.5 — Public playground** (static demo page shipped; live runner deferred behind hosting / rate-limiter / billing decisions)
  - `docs/playground.md` — pre-recorded real `cs review` / `dev` / `plan` / `debug` outputs so visitors can read the verdict shape, diff format, hypothesis ranking, and usage / cache-hit-rate banner before installing. No JS, no hosting cost, deploys via the existing GitHub Pages workflow.
  - "Why no live runner" footer is honest about the cost calculus: every visitor needs an API key or a shared bucket with a rate-limiter / abuse-mitigation queue / billing line. Replaced when W7.5b (hosted bucket) lands.
  - Mkdocs nav adds Playground as a top-level entry right after Home so it's the first thing a docs-site visitor sees after the landing page.
  - `playground.claudestruct.dev` with read-only sample runs, no key required
  - Limited to a 10k-token-per-day shared bucket; rate-limited per IP
- [~] **W7.6 — Marketing + docs site upgrade** (landing-page + nav rewrite shipped; Algolia DocSearch + demo videos + case studies deferred)
  - `docs/index.md` rewritten as a proper landing page: badges row, "Pick your path" tabbed quickstart (3 personas — solo dev / team / GX10 home-server), "What ships in the box" feature table, "Provider matrix" comparing `cs` vs `claw-squad` support across 7 providers + the hybrid-routing cost-math callout, "Why prompt caching matters" explainer with the 10× number, links to playground / install / cs guide, project-status badges, "Where to start" persona table.
  - Mkdocs nav reorganized: Playground promoted to top-level (after Home) so first-time visitors see the demo before drilling into reference docs.
  - Live demo videos / case studies / Algolia DocSearch are content + service work that lands when the project has a marketing budget; explicitly deferred.
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
- [x] **W8.2 — Billing & subscription** ✅ (skeleton + token-cap enforcement + live Stripe checkout + webhook handlers + invoice PDF passthrough all shipped)
  - `src/claudestruct/server/billing.py` — `Subscription` model (one row per org, `free`/`team`/`business` tier), `get_or_default()` lazy-materializes a free placeholder, `current_period_bounds()` falls back to the UTC calendar month when Stripe state is absent, `AUDIT_RETENTION_DAYS` map drives W8.4 pruning.
  - Routes: `GET /v1/billing/subscription` (viewer+), `POST /v1/billing/checkout` (admin), `GET /v1/billing/usage` (viewer+), `POST /v1/billing/webhook` (unauthenticated, signature-verified).
  - Stripe SDK lazy-imported via `stripe_sdk_available()`; checkout returns a deterministic stub URL on the OSS path; webhook returns 503 with a clear "install stripe" message. `billing.checkout.create` writes an audit row under the caller's org chain (W8.4 integration).
  - **Token-cap enforcement (this PR)**: `TIER_TOKEN_CAPS` (free=100k, team/business=None) + `tier_token_cap()` lookup with defensive free-fallback for unknown tiers; `current_period_token_usage(session, org_id)` sums `Run.input_tokens + output_tokens` over the org's current billing window (Stripe `current_period_*` if set, else UTC calendar month). `_enforce_token_cap()` in `routers/runs.py` rejects `POST /v1/runs` with **HTTP 402 Payment Required** + structured `TokenCapExceededResponse` body (`detail`, `used_tokens`, `cap_tokens`, `period_end`, `tier`) when an org has met or exceeded its cap. Failed runs still count toward the cap (the Anthropic API call already happened). Implicit-free orgs (no Subscription row) and orgs with unknown tier strings both fall back to free semantics — no path can accidentally bypass the gate.
  - Tests: `tests/test_billing.py` (17 prior + 4 new — `tier_token_cap` table + unknown-tier fallback + `current_period_token_usage` sums incl. failed runs + tenant isolation + window filtering); `tests/test_server.py` (8 new — under-cap 202, at-cap 402 with body shape, team-tier unlimited, business-tier unlimited, failed-runs count, unknown-tier-falls-back-to-free, no-Subscription-row treats as free, cross-org usage doesn't leak). Total Python: 329 passed; ruff clean.
  - **Invoice PDF passthrough (this PR)**: `GET /v1/billing/invoices/{id}/pdf` returns a JSON `InvoicePdfResponse {invoice_id, invoice_pdf_url, hosted_invoice_url}` so frontends can open the signed Stripe URL in a new tab without the browser dropping the bearer token across a 302. Tenant isolation: org with no `stripe_customer_id` → 404 (not 403); invoice whose `customer` doesn't match → 404 (not 403) so the endpoint can't be used as a customer-id enumeration oracle. `stripe.InvalidRequestError` (unknown / malformed ID) → 404; `stripe.StripeError` (network blip / Stripe outage) → 502 so the frontend can distinguish transient-upstream from unknown-id; draft invoice with no PDF yet → 409 (not 404) so the frontend retries instead of hitting a 404 page. 8 new tests in `tests/test_billing.py`: 503-no-stripe, 401-unauth, 404-no-customer, happy-path, cross-tenant 404, unknown-id 404, upstream-error 502, draft-invoice 409.
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
- [~] **W8.7 — Status page + SLO dashboard** (fleet-wide + per-tenant SLO + API latency middleware shipped; external status page deferred)
  - `src/claudestruct/server/slo.py` folds the `runs` table into rolling 24h / 7d / 30d windows; emits `success_rate`, `error_rate`, p50/p95/p99 of run-start latency (`started_at - created_at`) and duration (`duration_ms` for `done` runs only). Targets (`SUCCESS_RATE_TARGET=0.999`, `P95_RUN_START_MS_TARGET=5000`, `P95_DURATION_MS_TARGET=600000`) live as code-reviewed constants
  - `GET /v1/slo` is unauthenticated like `/healthz` so an external status page can scrape without a service token; output is aggregate (no run IDs / payloads / per-tenant data)
  - Failed runs excluded from duration percentiles (a single crash shouldn't poison p95); negative run-start deltas clamp to 0 (clock-skew defense); `null` percentiles surface for empty windows so consumers render "n/a" not 0
  - `docs/slo.md` documents the targets, the response shape, what's measured (and what isn't), and the status-page traffic-light mapping
  - **Per-tenant SLO**: `GET /v1/slo/tenant` (viewer+) scopes the snapshot to `principal.org_id` via `compute_tenant_snapshot(session, org_id)`; returns `org_slug` + `tier`. Multi-org isolation verified.
  - **API request-latency middleware (this PR)**: `RequestLatencyMiddleware` in `latency.py` times every HTTP request via `time.perf_counter()` and records per-route p50/p95/p99 in a thread-safe `LatencyTracker`. Route resolved via `getattr(route_obj, "path")` to avoid FastAPI's unhashable Route object. `GET /v1/slo/latency` (unauthenticated) returns Prometheus text exposition format suitable for Prometheus scraping or node_exporter textfile collector.
  - Tests: `tests/test_slo.py` (28 cases) — `_percentile` math, windowing, success/error rate, run-start latency, duration excludes failed runs, endpoint shape + region header, per-tenant empty/multi-org isolation, latency middleware records routes, latency endpoint returns Prometheus text.
  - Pending: external `status.claudestruct.dev` (manual infra), public incident timeline

---

## Wave 9 — Local-inference / GX10 ready (Q1+, opt-in)

Goal: make claudeStruct usable end-to-end on a local AI workstation (Asus GX10 / NVIDIA DGX Spark, 128GB unified memory) with zero per-token cost. Each item below is independently mergeable; pick by value/effort.

### Tier 1 — unblock the box

- [x] **W9.1 — claudestruct OpenAI-compat provider** ✅ (shipped as W10.1; see Wave 10 entry)
- [x] **W9.2 — Always-on home-server packaging** ✅ (shipped as W10.2; see Wave 10 entry)
- [x] **W9.3 — claw-squad GX10 preset config** ✅ (shipped as W10.3; see Wave 10 entry)

### Tier 2 — exploit local-only capabilities

- [x] **W9.4 — Local prompt-cache (content-hashed)** ✅ (storage + CLI wiring + provider integration all shipped)
  - `src/claudestruct/local_cache.py`: SHA-256 of `(provider, model, system, messages)` → cached `(text, usage, created_at)` blob in `~/.claudestruct/llm_cache/<aa>/<sha>.json` (two-level shard for scale). Atomic write-tmp-then-rename; corruption treated as miss (single bad file can't poison reads).
  - `is_enabled_for(provider_name, override=...)` resolves CLI override → env var → default policy (`auto`: on for openai-compat, off for anthropic — anthropic SDK already negotiates server-side cache so a local cache layered on top would surprise users with stale text).
  - `stats()` + `clear()` for `cs dashboard` integration.
  - **CLI flag wiring**: `--llm-cache` (force on) + `--no-llm-cache` (force off) on every task subcommand (`cs dev`, `cs review`, `cs plan`, `cs debug`). Threads through `cli.py` → `runner.run_task` → `client.call_claude_one_shot` → `local_cache.is_enabled_for(..., override=llm_cache)`. Default (no flag) → `None` so the env-driven `auto` policy stays in charge.
  - 30 prior storage-level tests + 5 runner-level tests in `tests/test_run_task_cache.py` + **3 new e2e CLI tests in `tests/test_e2e_cli.py`** locking the CLI flag plumbing (`--llm-cache` → `"on"`, `--no-llm-cache` → `"off"`, no-flag → `None`).

- [x] **W9.5 — RAG-style smart context gathering** ✅ (shipped as W10.5; see Wave 10 entry)

- [x] **W9.6 — Dataset export for fine-tuning** ✅ (both sides shipped: Python as W10.6, TS via `claw-squad dataset export`)
- [x] **W9.7 — Voice REPL (`cs voice` / `claw-squad voice`)** ✅ (Python side shipped as W10.7; claw-squad TS-side deferred)

### Tier 3 — operational polish

- [x] **W9.8 — Nightly code-health 守門員** ✅ (shipped as W10.8; see Wave 10 entry)
- [x] **W9.9 — claw-sandbox 真網路隔離 default-on under Linux** ✅ (shipped as W10.9; see Wave 10 entry)
- [x] **W9.10 — Hybrid cloud-local mode (config-only)** ✅ (shipped as W10.10; see Wave 10 entry)

### Reopen criteria

Order matters: W9.1 must land before any `cs *` subcommand can run on GX10. W9.4 + W9.5 both depend on W9.1 (they hook into the LLM call path). W9.6 / W9.7 / W9.10 are independent. W9.8 depends on W6.5 (already shipped).

---

## Optional Wave 10 — Enterprise top-up (deferred until Wave 8 lands enterprise contracts)

- SAML 2.0 SSO (Okta, Azure AD)
- SCIM 2.0 user provisioning
- Tenant-isolated VPC peering
- SOC2 Type II prep (gap analysis once Wave 5+8 controls are in place)
- HIPAA-eligible deployment
- Air-gapped self-hosted edition (Helm chart for fully-isolated installs)

Reopen criterion: a signed enterprise contract or three serious leads asking for any item above.

---

## Wave 10 — GX10 / local-first (Asus GX10: 128 GB unified memory, always-on, Linux)

Goal: turn the existing multi-tool stack into a first-class local-AI workstation experience that **doesn't need cloud Anthropic to be useful**. The Asus GX10's 128 GB unified memory + always-on Linux is the hardware target; items are ranked by how directly they exploit those traits.

### Tier 1 — Hits GX10's strengths head-on

- [x] **W10.1 — claudestruct OpenAI-compat client** ✅
  - New `src/claudestruct/providers.py` with `Provider` Protocol + `AnthropicProvider` (preserves the existing `cache_control: ephemeral` breakpoint exactly) + `OpenAICompatProvider` (chat-completions wire format with injectable `base_url`)
  - Selection at runtime via env: `CLAUDESTRUCT_PROVIDER=anthropic|openai`, `CLAUDESTRUCT_BASE_URL=...`, `CLAUDESTRUCT_MODEL_DEFAULT=...`. Unknown values fall back to anthropic so typos don't break unrelated commands
  - `client.py` refactored to dispatch through the Protocol; `RunResult` gains a `provider` field; cache telemetry only fires on the Anthropic path so the silent-miss detector stays quiet on local runs
  - OpenAI provider: streams via `chat.completions.create(stream=True, stream_options={"include_usage": True})`, maps `prompt_tokens`/`completion_tokens` into the existing `RunResult` shape, forwards `effort` as `reasoning_effort` only when set, falls back to chars/4 for `count_tokens` when tiktoken isn't importable
  - New `[openai]` extra in `pyproject.toml` brings `openai>=1.50` + `tiktoken>=0.7`. Without it, `CLAUDESTRUCT_PROVIDER=openai` raises a clear `_MissingDepError` pointing at the extra
  - Tests: 14 new cases in `tests/test_providers.py` covering selection / defaults / OpenAI message shape / tokenization fallback / streaming aggregator / `reasoning_effort` gating / soft-dep gating / Anthropic cache-breakpoint regression guard / dispatch / cache telemetry

- [x] **W10.2 — Always-on home / company control plane** ✅
  - `deploy/systemd/claudestruct.service` (HTTP API) + `deploy/systemd/claudestruct-worker.service` (queue drainer) ship as drop-in unit files, with `claudestruct` system user + `/var/lib/claudestruct` data dir + `EnvironmentFile=-/etc/claudestruct/claudestruct.env` for secrets. `ExecStartPre=/usr/local/bin/cs serve init-db` makes first-boot idempotent. Hardened with `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=…`, restricted address families, and `SystemCallArchitectures=native`
  - `claudestruct-worker.service` is `PartOf=claudestruct.service` with `TimeoutStopSec=300s` so an in-flight LLM call gets to finish before SIGKILL
  - `deploy/systemd/claw-squad.service` covers the optional Web UI (separate `clawsquad` user, port 8788)
  - `docs/home-server.md` walks the GX10-style host through install → service-user → secrets → systemd → Tailscale Funnel (single-path public webhook, rest tailnet-only) **or** Caddy reverse proxy (Let's Encrypt + private-IP allowlist) → org/user/key bootstrap → log streaming → upgrade flow. Calls out the explicit non-goals (HA, multi-worker, TLS-on-loopback)
  - `deploy/systemd/README.md` is the quick-install crib sheet
  - Pure ops: zero Python / TypeScript / Go changes. Wave 6–8 scaffolding (multi-tenant, GitHub App, SLO, cost-regression alerts) finally gets a 24/7 home

- [x] **W10.3 — Per-role local-model presets for claw-squad** ✅
  - `claw-squad/configs/{local-gx10,local-laptop,hybrid}.json` ship next to the binary; `--preset <name>` resolves them through `presetPath()` in `src/config.ts`
  - Precedence: defaults → preset → user `.claw-squad/config.json` → CLI flags. Subagent catalog is fully replaced (not merged) when user config defines `subagents` so per-team customisation isn't surprised by zombie entries
  - GX10 preset: Planner=`qwq:32b`, Coder=`qwen2.5-coder:32b`, Reviewer=`qwen2.5:7b`, plus a `research-helper` subagent on `llama3.2:3b`. All at `http://localhost:11434/v1` (Ollama default)
  - Laptop preset: smaller 14B Coder + Planner that fit in 32 GB without swapping; no subagents
  - `claw-squad/configs/README.md` explains the rationale + the `ollama pull …` recipe for the four-model GX10 set
  - Tests: 5 new vitest cases (`gx10`/`local-laptop`/`hybrid` round-trip, CLI flags win, user config wins, subagents replace not merge, unknown preset raises)

### Tier 2 — Things Anthropic can't do (but a beefy local box can)

- [x] **W10.4 — Local prompt-result cache (content-hashed)** ✅
  - Storage primitives shipped earlier as W9.4; this PR completes the wiring
  - `cache_key()` extended with `effort` + `max_tokens` so a `--effort low` replay never silently serves a `--effort max` response. Defaults `(None, 0)` keep pre-W10.4 callers working
  - New `is_enabled_for(provider_name, override=None)` policy: `auto` (default) → ON for openai-compat, OFF for Anthropic (the SDK already negotiates server-side ephemeral caching). `CLAUDESTRUCT_LLM_CACHE=on|off` env + per-run `--llm-cache` / `--no-llm-cache` flags override
  - `client.run_task` short-circuits on hit, replays the body through `stream_callback` once, returns `RunResult(cached=True, stop_reason="cached")`. New `cached: bool = False` field on `RunResult`
  - `runner.run_task_and_log` threads `llm_cache` through, sets `claudestruct.local_cache_hit` span attr, emits a structured `cache.local_hit` JSONL event so dashboards / alerts can count replays separately from real LLM calls
  - CLI: `--llm-cache` (force on) / `--no-llm-cache` (force off) on every `cs dev/review/plan/debug`. `_render_usage` shows a green `(cached)` badge when served from disk
  - Cache write is best-effort: `OSError` is logged to stderr and swallowed — the user's run never aborts because the cache failed to grow
  - Tests: 46 cases — `is_enabled_for` policy matrix (auto/Anthropic, auto/OpenAI, explicit on/off, per-run override wins), `cache_key` extensions (effort/max_tokens differs, default-stable), integration (first call misses → write, second hits → no provider call, stream replay still fires, `--no-llm-cache` bypass, Anthropic auto-OFF, opt-in works on Anthropic, write-failure non-fatal)

- [x] **W10.5b — claw-squad smart-context (index foundation)** ✅
  - TS mirror of W10.5's index pieces — biggest leverage on GX10 because local 32B Coders have 32K context windows vs. cloud Sonnet's 200K, so file selection quality matters more
  - `claw-squad/src/index/embed.ts`: `EmbeddingClient` over Node 18 `fetch` (zero new deps); env-driven defaults (`CLAW_SQUAD_EMBED_BASE_URL` / `_MODEL` / `_API_KEY`); error wrapping (HTTP status / unreachable / non-JSON / missing data / row-count mismatch)
  - `claw-squad/src/index/store.ts`: JSONL-on-disk store at `~/.claw-squad/index/<repo-fingerprint>.jsonl`. Per-repo SHA-256-fingerprint. Pure-JS cosine. Defends against zero-norm rows + dim-mismatch (raises rather than silently truncating). Atomic commit (write tmp + rename). Sorted output for byte-identical reproducibility. Skips corrupt JSONL lines on load. Chose JSONL over `better-sqlite3` to keep the smart-context surface `pnpm install`-able on every platform claw-squad already runs on
  - `claw-squad/src/index/build.ts`: walks tracked source files via the same `git ls-files` context-gather already uses, sha-skip on second build, 16-input embedding batches, 16 KB per-file cap matching the Python side
  - CLI: `claw-squad index build/stats/clear` subcommands; embedding-endpoint failure exits 2 with a clear `EmbeddingError` message
  - 28 vitest cases: repo fingerprint stability + per-path uniqueness, indexPath env override, store round-trip + reopen, upsert replace, no-op commit, clear, corrupt-line skip, deterministic-output sort, cosine ranking (3-point unit-circle, top-K respect, zero-vector empty, zero-norm skip, dim-mismatch raises), file_sha256 stability, embedding wire format (empty short-circuit, OpenAI-shape POST, non-2xx wrap, missing data, length mismatch), env passthrough, indexer (walks + embeds, sha-skip, re-embed-after-edit, non-git-repo gives 0-walked), smart-paths (top-K + empty-index)
  - `claw-squad/docs/smart-context.md` covers setup, build/stats/clear, JSONL-vs-SQLite rationale, follow-ups (orchestrator integration, cross-tool sharing, watch mode)
  - 399 vitest tests pass (was 371 → +28); `tsc --noEmit` + `pnpm build` clean
  - **Orchestrator integration shipped**: new `RunConfig.smartContext` field + `--smart-context` flag on `claw-squad run`. For each TODO, the orchestrator queries the index with `title + "\n" + description`, takes top-K (k=20), and threads them into `gatherInitialContext` via a new `extraExplicitPaths` option. Smart-context paths jump ahead of the keyword-rank list but after literal-mention paths (the user typing `foo.ts` is stronger evidence than the embedding model agreeing). Unknown / `git rm`'d paths from a stale index get silently dropped — the gatherer cross-references against `git ls-files` so a stale row can't fabricate a "missing file" entry. Embedding endpoint failure logs a one-liner and falls back to keyword rank — runs never abort because of smart-context alone. 3 new vitest cases (front-of-list bias, stale-path defensive drop, dedupe vs. literal mentions); 402 total tests pass
  - **Reviewer-side integration shipped**: `agents/reviewer.ts` accepts an optional `siblingContext` slot rendered above the diff under a "Sibling files (context only — NOT part of the diff)" heading. The orchestrator gathers it via `smartPaths` then dedupes against paths the diff already covers (parsed via `extractDiffPaths` — handles `diff --git`, `--- a/` / `+++ b/`, renames, `/dev/null` for create/delete). `readReviewerSiblings` applies tighter Reviewer-specific budgets (4 files / 32 KB total / 12 KB per file) than the Coder's, since the diff is the main feed. Embedding-endpoint failure logs a one-liner and the Reviewer sees the diff alone. 11 new vitest cases in `tests/reviewer-context.test.ts` (diff-path extraction matrix incl. renames + /dev/null, file/byte cap matrix, directory skip, empty-list); 413 total tests pass

- [x] **W10.5c — `cs --smart-context` graceful fallback** ✅
  - Polish gap surfaced once W10.5b shipped: the Python CLI exited 2 on `EmbeddingError` and warned-but-continued with `explicit=[]` on an empty index, while the TS-side `claw-squad run --smart-context` already had a unified best-effort path. Aligned the two.
  - `cli._run_common`: catch `EmbeddingError` → yellow warning + `explicit=None` so the gatherer falls back to its default candidate set (changed files / diff / keyword rank). Empty top-K from `smart_paths` follows the same path. Run never aborts because of smart-context alone.
  - 7 tests in `tests/test_smart_context_fallback.py` (autouse fixture stubs `count_tokens` so `--dry-run` doesn't reach Anthropic): EmbeddingError without flag (no warning), EmbeddingError with flag (warning mentions "falling back"), empty-index path (warning mentions `cs index build`), parametrized across all 4 task commands. 526 tests pass (was 519 → +7); ruff clean
  - `docs/smart-context.md` failure-modes table reworded to reflect best-effort posture; new explicit "best-effort" note matching the TS behaviour callout

- [x] **W10.5e — Cross-tool index export/import (`cs` ↔ `claw-squad`)** ✅
  - Closes the explicitly-deferred follow-up: both tools now share embeddings via a common JSONL bridge so a fresh checkout pays the embedding cost once, not twice.
  - **Python side** (`src/claudestruct/index_io.py` + `cs index export/import`): SQLite ↔ JSONL converters. `Index.iter_entries()` yields rows in sorted-by-path order (deterministic byte-identical output across runs). Schema: `{relPath, sha256, embedding}` per line — same keys the TS side already uses natively. `ensure_ascii=False` so non-ASCII paths render as themselves rather than `\u` escapes (smaller files, friendlier to grep).
  - **TS side** (`claw-squad/src/index/io.ts` + `claw-squad index export/import`): mirror module with the same schema + skip-malformed semantics. `Index.iterEntries()` for symmetry with Python's `iter_entries`.
  - Both `import` paths skip malformed lines (truncated transfer, hand-edits, non-numeric embedding values) + count them in the returned stats; one bad line never aborts. Embedding values preserved exactly through JSON round-trip — no float lossy conversion that would corrupt cosine ranking.
  - **Real cross-tool round-trip verified**: `cs index build` → `cs index export --out shared.jsonl` → manual `node` import on the TS side reads the file cleanly with `entries: 2, dimension: 2` and matching shas. Same in reverse.
  - 11 Python tests in `tests/test_index_io.py` + 11 vitest cases in `claw-squad/tests/index-io.test.ts`: round-trip, sorted output determinism, embedding fidelity, parent-dir creation, empty file, schema-shape contract (the keys on both sides), upsert semantics on key conflict, skip-malformed (broken JSON / non-dict / missing fields / non-numeric embedding), missing input file. 544 Python tests pass (was 533 → +11); 431 vitest tests pass (was 420 → +11). Type-check + build clean on both sides; ruff clean.
  - `docs/smart-context.md` (Python) + `claw-squad/docs/smart-context.md` (TS) both gain a "Cross-tool sharing" section with the bidirectional recipe.

- [x] **W10.5d (TS) — `claw-squad index watch`** ✅
  - Mirror of the Python-side W10.5d. `src/index/build.ts#watchIndex(repoRoot, {intervalS, maxIterations, sleep, onProgress})`: poll loop driven by `buildIndex`'s sha-skip — no new file-watcher dep. `EmbeddingError` from a single iteration is caught + reported via `onProgress`; the loop continues. Non-Embedding errors surface (don't silently get swallowed by the retry — a TypeError from a refactor must reach the operator).
  - `claw-squad index watch [--root .] [--interval 5]` CLI subcommand. SIGINT handler prints `watch stopped` and exits 0. Mirrors `cs index watch` behaviour byte-for-byte where possible.
  - 7 vitest cases in `tests/index.test.ts`: `maxIterations` bound, sleep injection (3 passes = 2 sleeps), sha-skip on second pass (`embed_calls` collapses to `[2]`), re-embed-after-edit (`[2, 1]`), `EmbeddingError` survival, non-Embedding TypeError surfaces, `maxIterations=0` short-circuit. 420 tests pass (was 413 → +7); `tsc --noEmit` + `pnpm build` clean.
  - `claw-squad/docs/smart-context.md`: new "Watch mode" section.

- [x] **W10.5d — `cs index watch` (Python side)** ✅
  - `indexer.watch_index(root, interval_s, max_iterations, sleep, progress)`: poll loop driven by `build_index`'s sha-skip — no new file-watcher dep. A no-op pass costs ~1 ms per tracked file (just stat + read) so the steady-state CPU cost on a 50k-file monorepo is negligible. `EmbeddingError` from a single iteration is logged + retried (Ollama restart / network blip never tears down a long-running watch).
  - `cs index watch [--root .] [--interval 5]` CLI subcommand. Renders progress + stats per pass via `err.print`. SIGINT gives a clean `watch stopped` line — no Python traceback in `journalctl`. `max_iterations` and `sleep` injection points keep the unit tests bounded.
  - 7 tests in `tests/test_index_watch.py`: `max_iterations` bound, sleep injection (3 passes = 2 sleeps), sha-skip on second pass (embed_calls = [2] for unchanged files), re-embed after edit (`[2, 1]` after a mid-loop write), `EmbeddingError` survival, `progress=None` doesn't crash, `max_iterations=0` short-circuit. 533 tests pass (was 526 → +7); ruff clean.
  - `docs/smart-context.md`: new "Watch mode" section covering interval semantics, transient-failure behaviour, Ctrl-C, and a hint to wire under systemd alongside the W10.2 units.

- [x] **W10.5 — RAG-style smart context with local embeddings** ✅
  - `src/claudestruct/embed.py`: stdlib-only OpenAI-compatible `/embeddings` POSTer (urllib + json). Defaults target `http://localhost:11434/v1` + `nomic-embed-text` (Ollama on a GX10); `CLAUDESTRUCT_EMBED_BASE_URL` / `_MODEL` / `_API_KEY` overrides cover cloud OpenAI / vLLM / SGLang / llama.cpp without code changes
  - `src/claudestruct/index.py`: SQLite store at `~/.claudestruct/index/<repo-fingerprint>.db` (one db per repo path, sha256-of-absolute-root keeps unrelated checkouts from thrashing), pure-Python cosine. Defends against zero-norm rows, dim-mismatch (`ValueError` rather than silent truncation), `CLAUDESTRUCT_INDEX_DIR` override
  - `src/claudestruct/indexer.py`: walks via existing `_walk_source_files` (gitignore-aware, source-extension allowlist), 16 KB per-file cap to keep request payload + sha-churn down, sha-skip on second build (`cs index build` becomes a no-op for unchanged files), batches 16 inputs per `/embeddings` call
  - CLI: `cs index build [--root .]` / `cs index stats` / `cs index clear`. New `--smart-context` flag on `cs dev/review/plan/debug` plumbs the description into a top-K (k=20) lookup, then falls into the existing gatherer with those paths as `explicit_paths` — per-task budget still enforced. Embedding-endpoint failure exits 2 with a clear message rather than silently degrading
  - `[smart-context]` extra in `pyproject.toml` (empty today; declared so docs can pin a stable install command and a future sqlite-vec drop-in lands without breaking anyone's pin)
  - 21 tests in `tests/test_index.py`: round-trip, replace-on-upsert, per-repo fingerprint, env-dir override, cosine ranking correctness (3-point unit-circle), top-K respect, zero-vector returns empty, zero-norm row skip, dim-mismatch raises, file_sha256 stability, indexer walk + sha-skip + re-embed-after-edit, smart_paths returns top-K + empty-index empty, embedding client env-passthrough + empty-input short-circuit
  - `docs/smart-context.md`: setup (Ollama + cloud OpenAI), build/stats/clear, failure-modes table, follow-ups (sqlite-vec, hybrid retrieval, claw-squad integration)
  - 496 Python tests pass (was 475)

- [x] **W10.6 — Reviewer fine-tuning from run-log history (Python side)** ✅
  - Existing logs are observability-only — no prompt + response capture. Added an opt-in `run.io` event in `logging.py` (gated on `CLAUDESTRUCT_LOG_PROMPTS=1`, default off for privacy) and emit it from `runner.run_task_and_log` after the LLM call. Response cap of 100 KB so a runaway model can't blow out the log file
  - `src/claudestruct/dataset.py` walks `.claudestruct/runs/*.jsonl`, yields `run.io` events, supports `--task` / `--since` filters, two output formats (`alpaca` `{instruction,input,output}` and `chat` `{messages: [...]}`), and is hardened against corrupt JSONL lines / missing run dirs / hand-edits
  - `cs dataset export --out PATH [--root .] [--task review] [--since YYYY-MM-DD] [--format alpaca|chat]` CLI subcommand. Empty result is not an error — writes a well-formed empty file so downstream pipelines don't have to special-case missing input
  - `scripts/finetune-reviewer.sh`: thin wrapper that exports the last-90-days dataset, drops a default axolotl YAML targeting Qwen2.5-Coder-7B QLoRA on a GX10, invokes `axolotl train`. Prints a clear warning when row count < 50 so the operator knows the LoRA won't actually learn anything yet
  - `docs/dataset-export.md`: privacy gate, flags table, train workflow, serve via W10.1's OpenAI-compat path, sanity-check `jq` recipes
  - 34 tests in `tests/test_dataset.py`: walker filter matrix, corrupt-line handling, sort-determinism, alpaca/chat format conversion, empty-result, unknown-format error, parent-dir creation, `parse_since` (date / ISO / Z-suffix / garbage), `run_io` schema + truncation cap, `_log_prompts_enabled` env-gate matrix
  - claw-squad-side `dataset export` shipped: `claw-squad/src/runs/dataset.ts` (walker + alpaca/chat conversion + `parseSince` mirroring the Python helper); new `run-io` event in `claw-squad/src/runs/log.ts`; `claw-squad dataset export --out PATH [--role coder] [--since YYYY-MM-DD] [--format alpaca|chat]` CLI subcommand. `makeRunIoEvent()` + `runIoEnabled()` (gated on `CLAW_SQUAD_LOG_PROMPTS=1`) + `emitRunIoIfEnabled()` helper called from each of `runPlanner` / `runCoder` / `runReviewer` after their `provider.invoke()` — orchestrator threads its existing `runLog` handle into each agent so the capture is end-to-end (no orchestrator code changes needed to enable; flip the env var). 36 vitest cases: walker filter matrix + corrupt-line handling + chronological sort + alpaca/chat conversion + empty-result + parent-dir creation + truncation + parseSince + 3 agent-integration cases (run-io emitted when gate on + runLog present, no emit when gate off, no crash when no runLog). `claw-squad/docs/dataset-export.md` covers prereqs, flags, cross-tool corpus combine recipe, implementation notes

- [x] **W10.7 — Voice REPL via local Whisper** ✅
  - `src/claudestruct/voice.py`: `VoiceConfig` dataclass (defaults target GX10 — `base.en`, 16 kHz mono, 5 s capture, auto device), `record_audio()` via sounddevice, `transcribe_audio()` via faster-whisper, `capture_and_transcribe()` orchestrator with `recorder` / `transcriber` injection points so tests skip the real audio + Whisper paths
  - Lazy-imports for `sounddevice` / `numpy` / `faster_whisper` — base install pays nothing; missing dep raises `VoiceError` pointing at `pip install 'claudestruct[voice]'` rather than the bare `ImportError` stack
  - Module-level `_MODEL_CACHE` keyed on `(model, device)` so repeated `cs voice` calls reuse loaded weights (~140 MB) instead of re-loading each time
  - CLI: `cs voice transcribe` (record + STT + stdout for piping) and `cs voice run <task>` (record + STT + invoke `cs dev/review/plan/debug` with the captured text). Common options `--seconds` / `--language` / `--model` / `--device`; `--print-only` on `run` for sanity-checking the mic without committing to an LLM call
  - `[voice]` extra in `pyproject.toml`: `faster-whisper>=1.0`, `sounddevice>=0.4`, `numpy>=1.24`
  - 11 tests in `tests/test_voice.py`: `VoiceConfig` defaults + override matrix, orchestrator injection (recorder + transcriber called with right args, default config when None, `VoiceError` propagation, empty-speech), lazy-import gating (both `_import_audio_deps` + `_import_whisper`), model cache (per-config caching + `reset_model_cache`), `transcribe_audio` segment concatenation + `extras` passthrough
  - `docs/voice.md`: setup, quick uses, flags table, traditional Chinese note, GX10 vs laptop latency table, failure-modes table, design rationale (why a subcommand not `--voice` on every task)
  - 507 Python tests pass (was 496); ruff clean
  - Closes W9.7 (duplicate of this item)

### Tier 3 — Push existing abstractions to the limit

- [x] **W10.8 — Nightly code-health watchdog** ✅
  - `scripts/nightly-review.sh` walks `git rev-list --max-count=$DEPTH origin/$BRANCH` and for each commit detaches HEAD, `git reset --soft <parent>`, runs `cs review --redact --log-json $LOG`, then resets back. Per-commit failures log to a sibling `.err` file but don't abort the loop — one bad merge shouldn't poison the night
  - Configurable via env: `CS_NIGHTLY_REPO` (required), `CS_NIGHTLY_BRANCH` (default `main`), `CS_NIGHTLY_DEPTH` (default `3`), `CS_NIGHTLY_LOG_DIR` (default `$REPO/.claudestruct/runs`)
  - `deploy/systemd/claudestruct-nightly.service` (Type=oneshot, same hardening profile as the daytime services) + `claudestruct-nightly.timer` (`OnCalendar=*-*-* 03:00:00`, `Persistent=true` so missed runs fire on next boot, `RandomizedDelaySec=15min`)
  - `docs/nightly-review.md` covers configuration, install (system + drop-in override), cron alternative, "why a wrapper script not a one-liner", and the morning `cs dashboard --root … --limit 20` routine
  - Pure ops: zero Python / TypeScript / Go changes. Plugs straight into the existing W6.5 cost-regression alerter

- [x] **W10.9 — Real network isolation for claw-sandbox on GX10** ✅
  - `tryDisableNetwork()` in `rlimit_linux.go` now sets `cmd.SysProcAttr.Cloneflags |= syscall.CLONE_NEWNET` when `isolationCapabilities().network` is `enforced` or `best-effort`. The kernel does the actual unsharing on `cmd.Start()`; no userspace `unshare` shim required
  - `shouldDefaultNoNetwork()` (Linux: `network == statusEnforced`; non-Linux: `false`) drives the new auto-on default. `resolveNoNetwork()` in `main.go` is the policy gate — explicit `--no-network` wins, explicit `--allow-network` opts out, both flags resolve to OFF with a stderr warning, neither delegates to the host capability
  - New `--allow-network` flag for `npm install` / `pip install` workflows. Existing scripts that pass `--no-network` still work; existing scripts that *don't* pass either flag get the new default ON only when running as root on Linux (the GX10 worker case) — non-root callers see no behaviour change
  - 4 new tests in `claw-sandbox/resolve_test.go` (explicit-on wins, explicit-off opts out, conflict treated as off, no-flags follows host policy)
  - `claw-squad/docs/sandbox-hardening.md` rewrites the network-isolation section with a host-config matrix and removes the "unsupported on every platform today" claim that's no longer true
  - Header comments in `main.go` updated; `--help` output now reflects the auto-on default and the new opt-out flag
  - Pure additive: zero behaviour change for hosts that can't actually enforce isolation

- [x] **W10.10 — Hybrid cloud / local routing** ✅
  - `claw-squad/configs/hybrid.json`: Planner on cloud Anthropic (`claude-opus-4-7`, asymmetric IQ demand + low call volume + prompt-cache savings); Coder + Reviewer on local Ollama (`qwen2.5-coder:32b` + `qwen2.5:7b`, high call volume + lower IQ ceiling)
  - `claw-squad/docs/hybrid-routing.md` walks through the cost math (~25 % of all-cloud spend on a 5-TODO run with 2 review rounds), failure modes (Ollama cold start, OOM mitigation, network split + `--resume` recovery), and migration path
  - Wired as the third entry in `PRESET_FILES`; `--preset hybrid` works out of the box. 1 new vitest case pins the role-by-role split
  - Per-role config existed since W1.x — this item was config + docs as planned

### Sequencing recommendation

Ship in roughly this order to maximize compounding value:
1. **W10.1** (OpenAI-compat client) — unblocks all the others on the Python side.
2. **W10.3** (preset) + **W10.10** (hybrid recipe) — both are tiny and immediately demonstrate the value of W10.1.
3. **W10.4** (local cache) — every subsequent dev-loop becomes faster.
4. **W10.2** (systemd / Tailscale docs) — required before W10.8 nightly + W6.6 webhooks have anywhere reliable to land.
5. **W10.9** (real netns isolation) — small change, big trust upgrade.
6. **W10.5** (RAG context) and **W10.6** (Reviewer fine-tune) — biggest engineering lifts; take them last when the feedback loops are tight.
7. **W10.7** (voice) and **W10.8** (nightly) — polish / habit features.

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

- 2026-05-01 — W10.4 (local prompt-result cache) wiring ready for PR push:
  - Storage primitives shipped earlier as W9.4; this PR completes the call-path wiring + provider-aware policy + CLI flags
  - `cache_key` now includes `effort` + `max_tokens` so quality knobs don't collide on replay; `is_enabled_for(provider, override)` defaults to ON for openai-compat / OFF for Anthropic (auto) but obeys env + per-run overrides
  - `RunResult.cached: bool` + green `(cached)` CLI badge + structured `cache.local_hit` event in the JSONL run log
  - 46 tests cover policy matrix + key collisions + first-miss-then-hit + stream replay + `--no-llm-cache` bypass + write-failure non-fatal. Total Python: 442 passed; ruff clean
- 2026-05-01 — Wave 10 first batch (W10.1 + W10.3 + W10.10) ready for PR push:
  - **W10.1 ✅** OpenAI-compat client — provider abstraction in `providers.py`, runtime selection via `CLAUDESTRUCT_PROVIDER`, new `[openai]` extra, 14 tests; cloud Anthropic path is byte-identical (cache breakpoint preserved)
  - **W10.3 ✅** claw-squad presets — `--preset {gx10,local-laptop,hybrid}` shipped alongside the binary under `configs/`, precedence layered below user config + CLI flags, 5 tests
  - **W10.10 ✅** hybrid cloud/local routing — `configs/hybrid.json` + `docs/hybrid-routing.md` (cost math, failure modes, migration path), 1 test
  - Total: 396 Python (+14 new) + 348 TS (+6 new) tests pass; ruff clean
  - Per the W10 sequencing recommendation, this knocks out the keystone (W10.1) plus both immediate compounding wins (W10.3 + W10.10) in one PR — local-first claudestruct is now functional on Ollama / vLLM / SGLang
- 2026-04-30 — Wave 10 (GX10 / local-first) added: 10 items targeting the Asus GX10 (128 GB unified memory, always-on Linux). Tier 1 (OpenAI-compat client / always-on control plane / per-role local presets) directly unblocks the existing stack on local hardware; Tier 2 (local cache / RAG context / Reviewer fine-tune / voice REPL) leverages the box's headroom; Tier 3 (nightly watchdog / real netns / hybrid routing) polishes existing abstractions. Sequencing recommendation embedded so future PRs pick the highest-leverage item next.
- 2026-04-28 — W7.1 VS Code extension scaffold ready for PR push:
  - New `vscode-extension/` standalone project (its own package.json + tsconfig + node_modules — separate publishing target from claw-squad's pnpm workspace)
  - 5 commands (Review / Dev / Plan / Debug / Dashboard) registered with right-click + palette entries; output streams to a single `claudeStruct` Output Channel
  - Pure-logic `runner.ts` split out for vitest coverage (13 cases passing, type-check clean) so the test suite doesn't need the VS Code API harness
  - Configurable `cliPath` / `maxBytes` / `effort` / `extraArgs` for venv setups + monthly-cap / log-json passthrough
  - Sideload-only for now (`vsce package` + `code --install-extension`); marketplace publish + SCM gutter integration tracked in the W7.1 follow-up
  - Total: 382 Python tests + 13 new VS Code vitest cases pass; ruff clean
- 2026-04-29 — claw-squad MCP server shipped:
  - `src/mcp/handlers.ts` with four read-only tools (`claw_squad_dashboard`, `claw_squad_dashboard_diff`, `claw_squad_runs_list`, `claw_squad_runs_purge`) — pure dict-in / dict-out so tests don't need the SDK
  - `src/mcp/server.ts` stdio bootstrap; lazy-imports `@modelcontextprotocol/sdk` (declared in `optionalDependencies` so default installs stay lean — mirrors the OTel pattern)
  - `claw-squad mcp` CLI subcommand
  - `docs/mcp.md` documents the tool surface, setup (`.mcp.json` example), and the rationale for shipping read-only first (full `claw_squad_run` deferred until streaming notifications are designed)
  - 18 new vitest cases. Total claw-squad: 327 passed; tsc clean
  - Pending: interactive `claw_squad_run` tool with streaming MCP notifications + non-interactive UI shim
- 2026-04-29 — W6.5 advance: notification surface + cost-regression alerts shipped:
  - `notify.py` (`Notifier` Protocol + `LogNotifier` + `SlackWebhookNotifier` + `default_notifier` env-driven factory)
  - `alerts.py` (`compute_cost_regression_alerts` mean+stddev detector, `finding_to_alert` severity ladder, `dispatch_findings` notifier glue)
  - `cs serve alerts` CLI subcommand for cron / Kubernetes CronJob driving
  - 25 new tests (detector correctness, severity ladder, both providers, factory). Total Python: 381 passed; ruff clean
  - Pending under W6.5: budget-cap rollups per team (same notifier surface), long-running scheduler, email provider
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
  - Pending under W8.2: invoice PDF passthrough
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
