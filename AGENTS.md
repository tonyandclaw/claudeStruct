# AGENTS.md

Repo overview for Codex (and humans) joining work mid-stream.

## Layout

- `src/claudestruct/` — Python CLI. One-shot Codex calls with smart context gathering and prompt caching. Entry: `src/claudestruct/cli.py`.
- `claw-squad/` — TypeScript orchestrator. 3 agents (Planner → Coder → Reviewer) over 8 providers. Entry: `claw-squad/src/cli.ts`.
- `claw-sandbox/` — Go binary that wraps subprocesses with rlimits + path validation + env scrubbing. Used by `claw-squad --sandbox`.
- `tests/` — Python pytest suite for claudestruct.
- `claw-squad/tests/` — vitest suite for claw-squad. ~24 files, 239 tests.

Detailed docs live under `claw-squad/docs/`:
- [agents.md](claw-squad/docs/agents.md) — agent interfaces, orchestrator state machine, adding a new agent
- [skills.md](claw-squad/docs/skills.md) — skill loading, `apply_to` matching, authoring
- [hooks.md](claw-squad/docs/hooks.md) — lifecycle hooks (preAgent/postAgent/preCommit/postCommit/onBudgetExceeded)
- [providers.md](claw-squad/docs/providers.md) — adding a new model backend

## Running

```bash
# claudestruct
pip install -e .
export ANTHROPIC_API_KEY=...
cs dev "your task"

# claw-squad
cd claw-squad
pnpm install         # or: npm install
pnpm build
node dist/cli.js run "your requirement"
```

## Testing

```bash
# Python
pytest

# TypeScript
cd claw-squad && pnpm test         # vitest run
cd claw-squad && npx tsc --noEmit  # type check

# Go
cd claw-sandbox && go test ./...
```

## Key conventions

- **Prompt caching is core.** Both tools mark the system prompt with `cache_control: ephemeral` (1h TTL). Anything that varies between calls (timestamps, UUIDs, dynamic context) must live in the user turn, not the system prompt — every byte change in the system block invalidates the entire cache prefix. The `--cache-warning` surfacing is automatic.
- **Prompts have content-hash versions** (sha256 prefix). Surfaced in usage output for traceability. `cs` prints `prompt: dev v=abc12345`; `claw-squad` prints `prompts: planner=... coder=... reviewer=...` in the run summary.
- **Structured logging via `--log-json <path>`.** Both tools emit JSONL events (`run.start`, `agent.usage`, `cache.warning`/`phase`, `run.end`) to a user-supplied path in addition to existing UI output. Schema is shared so a single consumer can aggregate both.
- **Config validation runs before agents start.** `claw-squad` uses zod (`src/config-schema.ts`) on the parsed `.claw-squad/config.json` — typos fail fast with a `agents.planner.effort` style path.
- **API retry/timeout** is delegated to the SDKs. We set `timeout` + `maxRetries` defaults and let the user override via env (`CLAUDESTRUCT_TIMEOUT`, `CLAW_SQUAD_TIMEOUT`, etc.). No hand-rolled retry loops.
- **Sandbox is defense-in-depth, not a VM.** `claw-sandbox` emits a `[sandbox] {"event":"isolation",...}` JSON line declaring per-control status. On macOS most controls are `unsupported`; on Linux rlimits land but `--no-network` needs CAP_SYS_ADMIN we don't assume. Run inside Docker / firejail for stronger guarantees.
- **State persists for `--resume`.** The orchestrator writes `.claw-squad/state.json` after every outer loop and in its `finally`. SIGINT routes through the same path so Ctrl-C doesn't lose progress.

## MCP setup (wire `cs` into Codex)

`cs mcp` runs claudestruct as an MCP server over stdio, exposing `claudestruct_dev` / `_review` / `_plan` / `_debug` / `_dashboard` / `_metrics` as tools. Codex (and any other MCP client) can then call these directly — no shell hop, prompt caching still applies, and the structured run log keeps recording every invocation.

Drop the following into Codex's `.mcp.json` (or the user-scope MCP config):

```json
{
  "mcpServers": {
    "claudestruct": {
      "command": "cs",
      "args": ["mcp"],
      "env": { "ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY" }
    }
  }
}
```

The handlers live in [src/claudestruct/mcp_handlers.py](src/claudestruct/mcp_handlers.py) — pure dict-in/dict-out functions that Codex's MCP client invokes. The server bootstrap in [src/claudestruct/mcp_server.py](src/claudestruct/mcp_server.py) is a thin wrapper around the SDK; lazy-imported so non-MCP runs don't pay the dep cost.

## Roadmap

[TODO.md](TODO.md) tracks the wave-by-wave roadmap (W1–W3 across stability, observability, DX, and new features). Updated on every PR push.
