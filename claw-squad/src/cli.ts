#!/usr/bin/env node
/**
 * claw-squad CLI entry.
 *
 * `claw-squad run "<requirement>"` kicks off the full 3-agent loop.
 * `claw-squad init` scaffolds .claw-squad/ in the current repo.
 *
 * Each agent's provider/model/baseURL can be set in three ways (highest
 * precedence last):
 *   1. Built-in defaults (all Anthropic)
 *   2. .claw-squad/config.json file
 *   3. CLI flags: --<role>-provider / --<role>-model / --<role>-base-url /
 *      --<role>-effort
 */

import { Command } from "commander";
import pc from "picocolors";
import prompts from "prompts";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { runOrchestrator, type UserInterface } from "./orchestrator.js";
import { installAbortSignal } from "./abort-signal.js";
import {
  loadAgentConfig,
  loadReposFromFile,
  type AgentCliOverride,
} from "./config.js";
import { loadSnapshot } from "./snapshot.js";
import {
  diffRuns,
  formatDiff,
  formatDiffJson,
  formatJson,
  formatTable,
  loadSummaries,
  loadSummaryById,
  watchSummaries,
} from "./dashboard.js";
import { loadHooksFromFile, NO_HOOKS, type Hooks } from "./hooks.js";
import { detectTestCommand } from "./test-runner.js";
import { ROLE_BUCKETS, type AgentRole, type RunConfig } from "./types.js";
import { loadPromptVersion } from "./prompts.js";
import {
  isSilentCacheInvalidator,
  type RunTotals,
} from "./totals.js";
import { VERSION } from "./version.js";

const program = new Command();

const ROLES: AgentRole[] = ["planner", "coder", "reviewer"];

program
  .name("claw-squad")
  .description(
    "3-agent Claude orchestrator: Planner -> Coder -> Reviewer. Supports Anthropic / OpenAI / Ollama / vLLM / SGLang / Gemini / MiniMax.",
  )
  .version(VERSION);

const runCmd = program
  .command("run")
  .description(
    "Run the 3-agent loop on a requirement (GitHub disabled by default).",
  )
  .argument("<requirement>", "the feature / task / question in plain language")
  .option("--root <path>", "repo root (defaults to cwd)", process.cwd())
  .option("--config <path>", "path to .claw-squad/config.json (otherwise auto-detected)")
  .option(
    "--preset <name>",
    "starting-point preset shipped with the binary. Available: gx10, local-laptop, hybrid. " +
    "User config + CLI flags still win on top.",
  )
  .option("--max-clarifications <n>", "max Planner Q&A rounds", "3")
  .option("--max-review-rounds <n>", "max Coder↔Reviewer rounds per task", "3")
  .option("--max-loops <n>", "max tasks to complete in one run", "10")
  .option(
    "--sandbox",
    "wrap Coder subprocess calls with claw-sandbox (Go binary). Default off.",
  )
  .option("--github", "actually push and PR to GitHub. Default off (dry local run).")
  .option("--github-repo <owner/repo>", "GitHub repo target when --github is set")
  .option("--no-self-learning", "disable memory/lessons.md writing")
  .option("--no-confirm", "skip human confirmation on destructive GitHub actions")
  .option(
    "--max-cost <usd>",
    "abort when estimated spend exceeds this many USD",
  )
  .option(
    "--max-tokens-total <n>",
    "abort when total tokens (input+output+cache) exceeds n",
  )
  .option("--resume", "resume from .claw-squad/state.json")
  .option(
    "--hooks <path>",
    "load a JS/TS module exporting lifecycle hooks (default export or `hooks` named export)",
  )
  .option(
    "--test-cmd <cmd>",
    "shell command to run after each Coder commit; failure feeds back to Coder",
  )
  .option(
    "--auto-test",
    "auto-detect a test command (npm test / pytest / go test / cargo test / ...)",
  )
  .option(
    "--test-timeout <ms>",
    "wall-clock cap for the test command (default 300000)",
  )
  .option(
    "--wait-for-ci",
    "after Reviewer approves, block on GitHub CI before merging",
  )
  .option(
    "--ci-timeout <ms>",
    "wall-clock cap for CI wait (default 900000)",
  )
  .option("--tui", "use the Ink-based terminal UI instead of plain streaming output")
  .option(
    "--slack-channel <id>",
    "post activity to a Slack channel (thread) instead of stdout. Needs SLACK_BOT_TOKEN env.",
  )
  .option(
    "--slack-mode <mode>",
    "Slack receiving transport: 'polling' (default) or 'socket'. Socket needs SLACK_APP_TOKEN env and gives real-time replies + Block Kit confirm buttons.",
  )
  .option(
    "--web-ui [port]",
    "serve a localhost web UI on the given port (default 3737)",
  )
  .option(
    "--web-ui-bind <host>",
    "host/interface for the web UI to bind. Default 127.0.0.1. Anything else (0.0.0.0, a LAN IP) requires --web-ui-token.",
  )
  .option(
    "--web-ui-token <token>",
    "shared secret required to connect to the web UI socket. May also be supplied via CLAW_WEB_TOKEN env.",
  )
  .option(
    "--no-rollback-on-max-rounds",
    "keep the task branch + PR when the Coder↔Reviewer loop hits max rounds (default: revert + close)",
  )
  .option(
    "--no-rollback-on-hard-fail",
    "keep the task branch on a preCommit hook abort (default: revert to starting ref)",
  )
  .option(
    "--dry-run",
    "stop after Planner finishes its TODO list and print a cost estimate; no Coder/Reviewer calls",
  )
  .option(
    "--log-json <path>",
    "mirror every run-log event (run-start, usage, phase, todo-complete, run-end) to this JSONL file in addition to .claw-squad/runs/",
  )
  .option(
    "--smart-context",
    "use the local embedding index (built via `claw-squad index build`) " +
      "to pick top-K relevant files for round 1 of each Coder task. " +
      "Biggest payoff on local 32B Coders with 32K context windows.",
  );

// Per-role provider flags. Commander can't easily do templated option
// names, so we add each explicitly. Keeping the name pattern stable
// (<role>-<knob>) so docs and completions are predictable.
for (const role of ROLES) {
  runCmd.option(
    `--${role}-provider <name>`,
    `provider for ${role} (anthropic | openai | gemini | minimax | ollama | vllm | sglang | openai-compat)`,
  );
  runCmd.option(`--${role}-model <id>`, `model for ${role}`);
  runCmd.option(`--${role}-base-url <url>`, `override base URL for ${role}'s provider`);
  runCmd.option(`--${role}-api-key <key>`, `override API key for ${role}'s provider`);
  runCmd.option(
    `--${role}-effort <level>`,
    `effort for ${role} (low | medium | high | xhigh | max)`,
  );
}

runCmd.action(async (requirement: string, opts: Record<string, unknown>) => {
    const config: RunConfig = {
      repoRoot: String(opts.root ?? process.cwd()),
      maxClarifications: Number(opts.maxClarifications),
      maxReviewRounds: Number(opts.maxReviewRounds),
      maxLoops: Number(opts.maxLoops),
      requireHumanApproval: opts.confirm !== false,
      sandboxEnabled: Boolean(opts.sandbox),
      selfLearning: opts.selfLearning !== false,
      githubEnabled: Boolean(opts.github),
      githubRepo: opts.githubRepo as string | undefined,
      maxCostUsd:
        opts.maxCost !== undefined ? Number(opts.maxCost) : undefined,
      maxTokens:
        opts.maxTokensTotal !== undefined
          ? Number(opts.maxTokensTotal)
          : undefined,
      testCommand: resolveTestCommand(opts),
      testTimeoutMs:
        opts.testTimeout !== undefined ? Number(opts.testTimeout) : undefined,
      waitForCi: Boolean(opts.waitForCi),
      ciTimeoutMs:
        opts.ciTimeout !== undefined ? Number(opts.ciTimeout) : undefined,
      // Commander sets opts.rollbackOnMaxRounds=false when --no-rollback-on-max-rounds
      // is passed. Default is undefined → treated as "on" by the orchestrator.
      rollbackOnMaxRounds: opts.rollbackOnMaxRounds !== false,
      rollbackOnHardFail: opts.rollbackOnHardFail !== false,
      dryRun: Boolean(opts.dryRun),
      logJsonPath:
        typeof opts.logJson === "string" ? opts.logJson : undefined,
      smartContext: Boolean(opts.smartContext),
    };

    // Pull multi-repo spec out of the config file if present. When
    // unset, the orchestrator falls back to the legacy single-repo
    // shape, so this is purely opt-in.
    try {
      const repos = loadReposFromFile({
        repoRoot: config.repoRoot,
        configPath: opts.config as string | undefined,
      });
      if (repos) config.repos = repos;
    } catch (err) {
      console.error(pc.red(`config error: ${(err as Error).message}`));
      process.exit(1);
    }

    // --github validation: when multi-repo is set, every repo must name
    // its githubRepo. Otherwise the legacy single-repo flag applies.
    if (config.githubEnabled) {
      if (config.repos && config.repos.length > 0) {
        const missing = config.repos.filter((r) => !r.githubRepo);
        if (missing.length > 0) {
          console.error(
            pc.red(
              `--github with multi-repo config requires githubRepo on each repo (missing: ${missing.map((r) => r.alias).join(", ")})`,
            ),
          );
          process.exit(1);
        }
      } else if (!config.githubRepo) {
        console.error(pc.red("--github requires --github-repo owner/name"));
        process.exit(1);
      }
    }

    let agentConfig;
    try {
      agentConfig = loadAgentConfig({
        repoRoot: config.repoRoot,
        configPath: opts.config as string | undefined,
        presetName: opts.preset as string | undefined,
        cliOverrides: extractCliOverrides(opts),
      });
    } catch (err) {
      console.error(pc.red(`config error: ${(err as Error).message}`));
      process.exit(1);
    }

    logConfig(config, agentConfig);

    // UI selection: TUI, Slack, Web UI, or plain CLI. At most one
    // remote/rich UI at a time — combining them creates confusing
    // behavior (whose `confirm` wins?) and we don't need it yet.
    const uiFlags = [
      opts.tui ? "--tui" : undefined,
      opts.slackChannel ? "--slack-channel" : undefined,
      opts.webUi !== undefined ? "--web-ui" : undefined,
    ].filter(Boolean);
    if (uiFlags.length > 1) {
      console.error(
        pc.red(
          `Pick one UI: ${uiFlags.join(", ")} are mutually exclusive.`,
        ),
      );
      process.exit(1);
    }

    let ui: UserInterface;
    let tuiInstance: { unmount(): void } | undefined;
    let remoteUi: { shutdown(): Promise<void> } | undefined;
    if (opts.tui && process.stdout.isTTY) {
      const { TuiUi } = await import("./tui/tui.js");
      const tui = new TuiUi();
      tui.mount();
      ui = tui;
      tuiInstance = tui;
    } else if (opts.slackChannel) {
      const { SlackUi, autodetectSlackMode } = await import("./ui/slack.js");
      // Honor --slack-mode if given; otherwise autodetect based on
      // which env tokens are set.
      let mode: "polling" | "socket";
      if (opts.slackMode === "polling" || opts.slackMode === "socket") {
        mode = opts.slackMode;
      } else if (opts.slackMode !== undefined) {
        console.error(
          pc.red(`--slack-mode must be 'polling' or 'socket' (got ${opts.slackMode})`),
        );
        process.exit(1);
      } else {
        mode = autodetectSlackMode();
      }
      if (mode === "socket" && !process.env.SLACK_APP_TOKEN) {
        console.error(
          pc.red(`--slack-mode socket requires SLACK_APP_TOKEN env`),
        );
        process.exit(1);
      }
      const slack = new SlackUi({
        channel: String(opts.slackChannel),
        openerText: `claw-squad starting: ${requirement.slice(0, 200)}`,
        mode,
      });
      await slack.ready();
      ui = slack;
      remoteUi = slack;
    } else if (opts.webUi !== undefined) {
      // `--web-ui` alone → default port 3737. `--web-ui 8080` →
      // Commander hands us "8080" as a string.
      const portArg =
        typeof opts.webUi === "string" ? Number(opts.webUi) : undefined;
      const hostArg =
        typeof opts.webUiBind === "string" ? opts.webUiBind : undefined;
      // CLI flag wins over env, both optional. The Web UI's
      // start() will hard-fail if hostArg is non-loopback without a
      // token, so we don't need to duplicate that check here.
      const tokenArg =
        (typeof opts.webUiToken === "string" ? opts.webUiToken : undefined) ??
        process.env.CLAW_WEB_TOKEN;
      const { WebUi } = await import("./ui/web.js");
      const web = new WebUi({
        port: portArg,
        host: hostArg,
        authToken: tokenArg,
      });
      try {
        const addr = await web.start();
        const hashHint = tokenArg ? `#token=${encodeURIComponent(tokenArg)}` : "";
        console.log(
          pc.cyan(`web UI listening on http://${addr.host}:${addr.port}/${hashHint}`),
        );
      } catch (err) {
        console.error(
          pc.red(`web UI failed to start: ${(err as Error).message}`),
        );
        process.exit(1);
      }
      ui = web;
      remoteUi = web;
    } else {
      if (opts.tui && !process.stdout.isTTY) {
        console.log(pc.yellow("[tui] stdout is not a TTY; falling back to plain CLI"));
      }
      ui = buildUI();
    }

    // Hooks (optional — defaults to no-op).
    let hooks: Hooks = NO_HOOKS;
    if (typeof opts.hooks === "string") {
      hooks = await loadHooksFromFile(opts.hooks, (m) => console.error(m));
    }

    // --resume: rehydrate SquadState from disk.
    let resumeFrom;
    let resumeTotals;
    if (opts.resume) {
      const snap = loadSnapshot(config.repoRoot);
      if (!snap) {
        console.error(
          pc.red(`no snapshot at .claw-squad/state.json — nothing to resume`),
        );
        process.exit(1);
      }
      resumeFrom = snap.state;
      resumeTotals = snap.totals;
      console.log(
        pc.cyan(
          `Resuming from ${snap.savedAt} — ${snap.state.todos.length} todos, loopCount=${snap.state.loopCount}, prior spend $${snap.totals.overall.costUsd.toFixed(4)}`,
        ),
      );
    }

    const abort = installAbortSignal({ ui });
    try {
      const result = await runOrchestrator({
        config,
        agentConfig,
        requirement,
        ui: abort.ui,
        hooks,
        resumeFrom,
        resumeTotals,
      });
      tuiInstance?.unmount();
      await remoteUi?.shutdown();
      printSummary(result);
      if (result.reason === "complete" || result.reason === "dry_run")
        process.exit(0);
      if (result.reason === "blocked" || result.reason === "aborted")
        process.exit(2);
      process.exit(3);
    } catch (err) {
      tuiInstance?.unmount();
      await remoteUi?.shutdown();
      console.error(pc.red(`\nFatal: ${(err as Error).message}`));
      process.exit(1);
    } finally {
      abort.dispose();
    }
  });

program
  .command("init")
  .description(
    "Create .claw-squad/ with a sample config.json showing provider options.",
  )
  .option("--root <path>", "repo root", process.cwd())
  .action((opts: { root: string }) => {
    const dir = join(opts.root, ".claw-squad");
    mkdirSync(join(dir, "memory"), { recursive: true });
    const cfgPath = join(dir, "config.json");
    if (!existsSync(cfgPath)) {
      writeFileSync(
        cfgPath,
        JSON.stringify(
          {
            agents: {
              planner: {
                name: "anthropic",
                model: "claude-opus-4-7",
                effort: "max",
              },
              coder: {
                // Example: point Coder at a local Ollama server.
                // Remove this comment and fill in your model name.
                // name: "ollama",
                // model: "qwen2.5-coder:14b",
                // baseURL: "http://localhost:11434/v1",
                name: "anthropic",
                model: "claude-sonnet-4-6",
                effort: "high",
              },
              reviewer: {
                name: "anthropic",
                model: "claude-opus-4-7",
                effort: "xhigh",
              },
            },
          },
          null,
          2,
        ) + "\n",
        "utf-8",
      );
    }
    console.log(pc.green(`Initialized ${dir}`));
    console.log(pc.dim(`Edit ${cfgPath} to change provider/model per agent.`));
  });

function extractCliOverrides(
  opts: Record<string, unknown>,
): Partial<Record<AgentRole, AgentCliOverride>> {
  const out: Partial<Record<AgentRole, AgentCliOverride>> = {};
  for (const role of ROLES) {
    const o: AgentCliOverride = {};
    const p = opts[`${role}Provider`];
    const m = opts[`${role}Model`];
    const b = opts[`${role}BaseUrl`];
    const k = opts[`${role}ApiKey`];
    const e = opts[`${role}Effort`];
    if (typeof p === "string") o.provider = p;
    if (typeof m === "string") o.model = m;
    if (typeof b === "string") o.baseURL = b;
    if (typeof k === "string") o.apiKey = k;
    if (typeof e === "string") o.effort = e;
    if (Object.keys(o).length > 0) out[role] = o;
  }
  return out;
}

function logConfig(
  c: RunConfig,
  agents: Awaited<ReturnType<typeof loadAgentConfig>>,
): void {
  console.log(pc.dim("─".repeat(60)));
  console.log(pc.bold("claw-squad run configuration"));
  console.log(`  root:            ${c.repoRoot}`);
  console.log(
    `  sandbox:         ${c.sandboxEnabled ? pc.yellow("ON") : pc.dim("off (default)")}`,
  );
  console.log(
    `  self-learning:   ${c.selfLearning ? pc.green("on") : pc.dim("off")}`,
  );
  console.log(
    `  github:          ${c.githubEnabled ? pc.yellow("ON") : pc.dim("off (dry run)")}`,
  );
  console.log(`  max clarifs:     ${c.maxClarifications}`);
  console.log(`  max review:      ${c.maxReviewRounds}`);
  console.log(`  max loops:       ${c.maxLoops}`);
  console.log("");
  console.log(pc.bold("agents"));
  for (const role of ROLES) {
    const a = agents[role];
    console.log(
      `  ${role.padEnd(9)} ${pc.cyan(a.name)} ${a.model} ${a.baseURL ? pc.dim(`(${a.baseURL})`) : ""}${a.effort ? pc.dim(` effort=${a.effort}`) : ""}`,
    );
  }
  console.log(pc.dim("─".repeat(60)));
}

function resolveTestCommand(opts: Record<string, unknown>): string | undefined {
  // Explicit --test-cmd wins. Fall back to --auto-test detection.
  if (typeof opts.testCmd === "string" && opts.testCmd.length > 0) {
    return opts.testCmd;
  }
  if (opts.autoTest) {
    const detected = detectTestCommand(String(opts.root ?? process.cwd()));
    if (detected) {
      console.log(pc.dim(`[auto-test] detected: ${detected}`));
    }
    return detected;
  }
  return undefined;
}

function buildUI(): UserInterface {
  return {
    async askClarifications(questions) {
      const answers: string[] = [];
      console.log(pc.cyan("\n[Planner asks:]"));
      for (const q of questions) {
        const { answer } = await prompts({
          type: "text",
          name: "answer",
          message: q,
        });
        answers.push(typeof answer === "string" ? answer : "");
      }
      return answers;
    },
    async confirm(prompt) {
      const { ok } = await prompts({
        type: "confirm",
        name: "ok",
        message: prompt,
        initial: false,
      });
      return Boolean(ok);
    },
    log(msg) {
      console.log(msg);
    },
    streamAgent(_role, chunk) {
      process.stdout.write(chunk);
    },
  };
}

function printSummary(result: {
  totals: RunTotals;
  reason: string;
}): void {
  const t = result.totals;
  const overall = t.overall;
  console.log(pc.dim("\n" + "─".repeat(72)));
  console.log(pc.bold("Usage summary"));

  // Per-role table. Only show rows with actual traffic so the output
  // stays readable for short runs.
  const col = (s: string, w: number) => s.padEnd(w);
  const num = (n: number, w: number) => n.toLocaleString().padStart(w);
  const dollars = (n: number, w: number) => ("$" + n.toFixed(4)).padStart(w);
  const header =
    col("  role", 14) +
    col("calls", 8) +
    col("in", 12) +
    col("out", 12) +
    col("cacheR", 12) +
    col("cost", 12);
  console.log(pc.dim(header));
  for (const role of ROLE_BUCKETS) {
    const r = t.perRole[role];
    if (r.calls === 0) continue;
    console.log(
      col(`  ${role}`, 14) +
        num(r.calls, 8).padEnd(8) +
        num(r.inputTokens, 12).padEnd(12) +
        num(r.outputTokens, 12).padEnd(12) +
        num(r.cacheReadTokens, 12).padEnd(12) +
        dollars(r.costUsd, 12).padEnd(12),
    );
  }
  console.log(
    pc.bold(
      col("  total", 14) +
        num(overall.calls, 8).padEnd(8) +
        num(overall.inputTokens, 12).padEnd(12) +
        num(overall.outputTokens, 12).padEnd(12) +
        num(overall.cacheReadTokens, 12).padEnd(12) +
        dollars(overall.costUsd, 12).padEnd(12),
    ),
  );

  // Cache ROI. cacheSavedUsd is computed per-call in totals.ts using
  // the provider rate table — only Anthropic contributes today.
  if (overall.cacheSavedUsd > 0) {
    const baseline = overall.costUsd + overall.cacheSavedUsd;
    const pct =
      baseline > 0 ? ((overall.cacheSavedUsd / baseline) * 100).toFixed(1) : "0.0";
    console.log(
      pc.green(
        `  cache savings:        $${overall.cacheSavedUsd.toFixed(4)} (${pct}% vs. no-cache)`,
      ),
    );
  }

  // Silent-cache-invalidator detector: many Anthropic calls, zero
  // reads. Either a nondeterministic prefix (timestamp/UUID) or the
  // tool set / system prompt is shifting between calls.
  if (isSilentCacheInvalidator(overall)) {
    console.log(
      pc.yellow(
        `  warning:              ${overall.anthropicCalls} Anthropic calls, 0 cache reads — prompt prefix may be invalidating the cache`,
      ),
    );
  }

  // Prompt versions — content hash of each role's system prompt,
  // shown so a cache regression or behavior shift can be tied to a
  // specific prompt revision.
  const planner = loadPromptVersion("planner");
  const coder = loadPromptVersion("coder");
  const reviewer = loadPromptVersion("reviewer");
  console.log(
    pc.dim(
      `  prompts:              planner=${planner} coder=${coder} reviewer=${reviewer}`,
    ),
  );

  console.log(`  outcome:              ${result.reason}`);
}

const dashboardCmd = program
  .command("dashboard")
  .description(
    "Print a cost/outcome table for every run logged under .claw-squad/runs/",
  )
  .option("--root <path>", "repo root", process.cwd())
  .option(
    "--filter <substring>",
    "case-insensitive filter on the requirement column",
  )
  .option(
    "--json",
    "emit machine-readable JSON instead of the human table (good for jq / spreadsheets)",
  )
  .option(
    "--watch [interval]",
    "re-render every N seconds (default 5). Ctrl-C to exit.",
  )
  .action(
    (opts: {
      root: string;
      filter?: string;
      json?: boolean;
      watch?: string | boolean;
    }) => {
      // --watch [interval] is a Commander variadic-ish optional arg.
      // Boolean true means the flag was passed without a value.
      if (opts.watch !== undefined) {
        const intervalMs =
          typeof opts.watch === "string" ? Number(opts.watch) * 1000 : 5_000;
        if (Number.isNaN(intervalMs) || intervalMs < 200) {
          console.error(pc.red(`--watch interval must be ≥ 0.2 seconds`));
          process.exit(1);
        }
        const handle = watchSummaries(opts.root, intervalMs, (summaries) => {
          // Clear screen + move cursor home; portable enough for any
          // ANSI-aware terminal. Plain stdout if NO_COLOR is set.
          if (!process.env.NO_COLOR) {
            process.stdout.write("c");
          }
          const rendered = opts.json
            ? formatJson(opts.filter ? filterByRequirement(summaries, opts.filter) : summaries)
            : formatTable(opts.filter ? filterByRequirement(summaries, opts.filter) : summaries);
          console.log(rendered);
          console.log(
            pc.dim(`\nwatching ${opts.root}/.claw-squad/runs/  •  Ctrl-C to exit`),
          );
        });
        process.on("SIGINT", () => {
          handle.stop();
          process.exit(0);
        });
        return;
      }

      const summaries = loadSummaries(opts.root, { filter: opts.filter });
      if (opts.json) {
        console.log(formatJson(summaries));
      } else {
        console.log(formatTable(summaries));
      }
    },
  );

dashboardCmd
  .command("diff <a> <b>")
  .description(
    "Compare two run IDs (filename without .jsonl). Shows cost / outcome / per-role / per-subagent deltas (b - a).",
  )
  .option("--root <path>", "repo root", process.cwd())
  .option(
    "--json",
    "emit a structured JSON diff instead of the human report",
  )
  .action(
    (
      a: string,
      b: string,
      opts: { root: string; json?: boolean },
    ) => {
      const aSummary = loadSummaryById(opts.root, a);
      const bSummary = loadSummaryById(opts.root, b);
      if (!aSummary) {
        console.error(pc.red(`run ${a} not found under ${opts.root}/.claw-squad/runs/`));
        process.exit(1);
      }
      if (!bSummary) {
        console.error(pc.red(`run ${b} not found under ${opts.root}/.claw-squad/runs/`));
        process.exit(1);
      }
      const report = diffRuns(aSummary, bSummary);
      console.log(opts.json ? formatDiffJson(report) : formatDiff(report));
    },
  );

// Local helper kept off the dashboard module because it's a pure
// rendering convenience — `loadSummaries` already supports filtering
// for the non-watch path, but watchSummaries returns the full set
// (cheaper than re-running the filter logic on every tick).
function filterByRequirement<T extends { requirement?: string }>(
  rows: T[],
  needle: string,
): T[] {
  const n = needle.toLowerCase();
  return rows.filter((r) => r.requirement && r.requirement.toLowerCase().includes(n));
}


// --- skills marketplace (W7.3) --------------------------------------

const skillsCmd = program
  .command("skills")
  .description("Marketplace for sharable skill packs (manifest + sha256 verified).");

skillsCmd
  .command("list")
  .description("List installed skills with their manifest provenance.")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (opts: { root: string }) => {
    const { listInstalled } = await import("./skills-registry.js");
    const installed = listInstalled(opts.root);
    if (installed.length === 0) {
      console.log(pc.dim("No skills installed under .claw-squad/skills/."));
      return;
    }
    for (const s of installed) {
      const v = s.manifest ? `v${s.manifest.version}` : pc.dim("(no manifest)");
      console.log(`${pc.cyan(s.id)}  ${v}`);
      if (s.manifest?.description) {
        console.log(`  ${pc.dim(s.manifest.description)}`);
      }
    }
  });

skillsCmd
  .command("install <idOrUrl>")
  .description(
    "Install a skill by id (resolved against the registry) or by direct manifest URL.",
  )
  .option("--root <path>", "repo root", process.cwd())
  .option(
    "--registry <url>",
    "Override the registry index URL (default: skills.claudestruct.dev).",
  )
  .action(async (idOrUrl: string, opts: { root: string; registry?: string }) => {
    const {
      DEFAULT_REGISTRY_URL,
      installSkill,
      loadRegistryIndex,
      parseManifest,
    } = await import("./skills-registry.js");
    let manifest: ReturnType<typeof parseManifest> | null = null;

    // Direct URL? Treat as a manifest URL (we fetch the manifest, then
    // the manifest tells us where the .md body lives). Otherwise look
    // up by id in the registry.
    if (idOrUrl.startsWith("http://") || idOrUrl.startsWith("https://") || idOrUrl.startsWith("file://")) {
      const isLocal = idOrUrl.startsWith("file://");
      const body = isLocal
        ? (await import("node:fs")).readFileSync(idOrUrl.slice("file://".length), "utf-8")
        : await (await fetch(idOrUrl)).text();
      manifest = parseManifest(JSON.parse(body));
    } else {
      const url = opts.registry ?? process.env.CLAW_SKILLS_REGISTRY ?? DEFAULT_REGISTRY_URL;
      const { manifests, warnings } = await loadRegistryIndex(url);
      for (const w of warnings) console.warn(pc.yellow(`[warn] ${w}`));
      const found = manifests.find((m) => m.id === idOrUrl);
      if (!found) {
        console.error(pc.red(`skill "${idOrUrl}" not found in ${url}`));
        process.exit(1);
      }
      manifest = found;
    }
    if (typeof manifest === "string") {
      console.error(pc.red(`invalid manifest: ${manifest}`));
      process.exit(1);
    }
    const res = await installSkill(opts.root, manifest);
    console.log(pc.green(`installed ${res.manifest.id}@${res.manifest.version}`));
    console.log(pc.dim(`  ${res.installedPath}`));
  });

skillsCmd
  .command("uninstall <id>")
  .description("Remove an installed skill (and its sidecar manifest).")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (id: string, opts: { root: string }) => {
    const { uninstallSkill } = await import("./skills-registry.js");
    const removed = uninstallSkill(opts.root, id);
    if (removed) {
      console.log(pc.green(`uninstalled ${id}`));
    } else {
      console.log(pc.dim(`${id}: nothing to remove`));
    }
  });


// --- run logs (W5.3 follow-up) -------------------------------------

const runsCmd = program
  .command("runs")
  .description("Inspect / prune the per-run JSONL logs under .claw-squad/runs/.");

runsCmd
  .command("list")
  .description("List run-log files (path, age, size).")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (opts: { root: string }) => {
    const { loadAllRuns } = await import("./runs/log.js");
    const { statSync } = await import("node:fs");
    const runs = loadAllRuns(opts.root);
    if (runs.length === 0) {
      console.log(pc.dim(`No runs under ${opts.root}/.claw-squad/runs/.`));
      return;
    }
    const now = Date.now();
    for (const r of runs) {
      let ageDays = 0;
      let sizeKb = 0;
      try {
        const st = statSync(r.path);
        ageDays = (now - st.mtimeMs) / (24 * 60 * 60 * 1000);
        sizeKb = st.size / 1024;
      } catch {
        /* skip stat errors */
      }
      console.log(
        `${pc.cyan(r.path)}  ${pc.dim(
          `${ageDays.toFixed(1)}d  ${sizeKb.toFixed(1)}KB`,
        )}`,
      );
    }
  });

runsCmd
  .command("purge")
  .description("Delete run-log files older than --older-than-days.")
  .option("--root <path>", "repo root", process.cwd())
  .requiredOption(
    "--older-than-days <n>",
    "Delete files whose mtime is older than this many days.",
  )
  .option("--dry-run", "List candidates without deleting them.", false)
  .action(
    async (opts: {
      root: string;
      olderThanDays: string;
      dryRun: boolean;
    }) => {
      const days = Number(opts.olderThanDays);
      if (!Number.isFinite(days) || days < 0) {
        console.error(pc.red(`--older-than-days must be a non-negative number`));
        process.exit(1);
      }
      const { purgeRuns, daysToMs } = await import("./runs/purge.js");
      const victims = purgeRuns(opts.root, {
        olderThanMs: daysToMs(days),
        dryRun: opts.dryRun,
      });
      if (victims.length === 0) {
        console.log(pc.dim("No run logs older than the cutoff."));
        return;
      }
      const verb = opts.dryRun ? "Would delete" : "Deleted";
      console.log(pc.bold(`${verb} ${victims.length} file(s):`));
      for (const p of victims) {
        console.log(`  ${p}`);
      }
    },
  );


// claw-squad smart-context — TS mirror of `cs index` (W10.5). Builds
// a local embedding index of tracked source files so the Coder /
// Reviewer can pick top-K semantic matches instead of keyword rank.
// The `--smart-context` flag on `claw-squad run` lands separately;
// this PR ships the index management surface.
const indexCmd = program
  .command("index")
  .description(
    "Manage the local embedding index for --smart-context. " +
      "Defaults to Ollama at http://localhost:11434/v1; override via " +
      "CLAW_SQUAD_EMBED_BASE_URL / _MODEL / _API_KEY.",
  );

indexCmd
  .command("build")
  .description("Walk tracked source files and (re-)embed each into the local index.")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (opts: { root: string }) => {
    const { buildIndex } = await import("./index/build.js");
    const { EmbeddingError } = await import("./index/embed.js");
    try {
      const stats = await buildIndex(opts.root, {
        onProgress: (msg) => console.log(pc.dim(msg)),
      });
      console.log(
        `walked ${stats.walked} file(s); ` +
          `embedded ${stats.embedded}; ` +
          `skipped ${stats.skippedUnchanged} unchanged, ${stats.skippedUnreadable} unreadable`,
      );
    } catch (err) {
      if (err instanceof EmbeddingError) {
        console.error(pc.red(`embedding endpoint failed: ${err.message}`));
        process.exit(2);
      }
      throw err;
    }
  });

indexCmd
  .command("stats")
  .description("Print row count + embedding dimension for the local index.")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (opts: { root: string }) => {
    const { Index } = await import("./index/store.js");
    const idx = Index.open(opts.root);
    const s = idx.stats();
    console.log(`entries: ${s.entries}`);
    console.log(`dimension: ${s.dimension}`);
  });

indexCmd
  .command("clear")
  .description("Drop every row from the local index.")
  .option("--root <path>", "repo root", process.cwd())
  .action(async (opts: { root: string }) => {
    const { Index } = await import("./index/store.js");
    const idx = Index.open(opts.root);
    const n = idx.clear();
    idx.commit();
    console.log(`deleted ${n} entr${n === 1 ? "y" : "ies"}`);
  });

indexCmd
  .command("export")
  .description("Dump the index to JSONL for cross-tool sharing with `cs index`.")
  .option("--root <path>", "repo root", process.cwd())
  .requiredOption(
    "--out <path>",
    "Output JSONL path. Created (or overwritten) by this command.",
  )
  .action(async (opts: { root: string; out: string }) => {
    const { exportToJsonl } = await import("./index/io.js");
    const stats = exportToJsonl(opts.root, opts.out);
    console.log(
      `exported ${stats.rows} entr${stats.rows === 1 ? "y" : "ies"} to ${stats.outputPath}`,
    );
  });

indexCmd
  .command("import <path>")
  .description(
    "Load JSONL (this tool's export, or cs index export) into the local index.",
  )
  .option("--root <path>", "repo root", process.cwd())
  .action(async (path: string, opts: { root: string }) => {
    const { importFromJsonl } = await import("./index/io.js");
    let stats;
    try {
      stats = importFromJsonl(opts.root, path);
    } catch (err) {
      console.error(pc.red((err as Error).message));
      process.exit(2);
    }
    const note = stats.skippedMalformed
      ? ` (${stats.skippedMalformed} malformed line(s) skipped)`
      : "";
    console.log(
      `imported ${stats.rows} entr${stats.rows === 1 ? "y" : "ies"} from ${stats.inputPath}${note}`,
    );
  });

indexCmd
  .command("watch")
  .description(
    "Keep the index warm: re-run `index build` on a fixed interval. " +
      "Mirror of `cs index watch`. Designed for the GX10 home-server " +
      "so `--smart-context` always sees today's tree.",
  )
  .option("--root <path>", "repo root", process.cwd())
  .option(
    "--interval <seconds>",
    "Seconds between passes. Default 5; bump for very large monorepos.",
    "5",
  )
  .action(async (opts: { root: string; interval: string }) => {
    const intervalS = Number(opts.interval);
    if (!Number.isFinite(intervalS) || intervalS <= 0) {
      console.error(pc.red("--interval must be a positive number"));
      process.exit(2);
    }
    const { watchIndex } = await import("./index/build.js");
    console.error(
      pc.bold(
        `watching ${opts.root} (interval ${intervalS.toFixed(1)}s) — Ctrl-C to stop`,
      ),
    );
    // SIGINT shows up as a process exit on Node; the watch loop is
    // an async generator that never resolves under normal use, so
    // we just let the signal end the process. Node prints no
    // traceback by default; mirror the Python side's clean stop
    // message via a SIGINT handler.
    process.on("SIGINT", () => {
      console.error(pc.bold("\nwatch stopped"));
      process.exit(0);
    });
    await watchIndex(opts.root, {
      intervalS,
      onProgress: (msg: string) => console.error(pc.dim(msg)),
    });
  });


// W10.6 — TS-side dataset export. Mirrors `cs dataset export` from
// claudestruct so a single team can mine both tools' run logs into
// one fine-tune corpus without writing custom scripts.
const datasetCmd = program
  .command("dataset")
  .description(
    "Mine the .claw-squad/runs/ JSONL logs for fine-tuning datasets (W10.6). " +
      "Requires runs that were captured with CLAW_SQUAD_LOG_PROMPTS=1.",
  );

datasetCmd
  .command("export")
  .description(
    "Walk .claw-squad/runs/*.jsonl and write a training-format JSONL.",
  )
  .requiredOption(
    "--out <path>",
    "Output JSONL path. Created (or overwritten) by this command.",
  )
  .option("--root <path>", "repo root", process.cwd())
  .option(
    "--role <bucket>",
    "Filter to one of: planner / coder / reviewer / subagent. Default: include all.",
  )
  .option(
    "--since <date>",
    "Filter to events on or after this date (YYYY-MM-DD or ISO 8601).",
  )
  .option<"alpaca" | "chat">(
    "--format <fmt>",
    "Output schema: 'alpaca' (default) = {instruction,input,output}; 'chat' = {messages: [...]}",
    (val): "alpaca" | "chat" => {
      if (val !== "alpaca" && val !== "chat") {
        throw new Error(
          `--format must be 'alpaca' or 'chat'; got ${JSON.stringify(val)}`,
        );
      }
      return val;
    },
    "alpaca",
  )
  .option<"approve" | "request_changes">(
    "--review-decision <decision>",
    "Only include events from runs whose Reviewer reached this decision (W11.5). " +
      "Choices: approve / request_changes. Older runs missing the field are dropped.",
    (val): "approve" | "request_changes" => {
      if (val !== "approve" && val !== "request_changes") {
        throw new Error(
          `--review-decision must be 'approve' or 'request_changes'; got ${JSON.stringify(val)}`,
        );
      }
      return val;
    },
  )
  .action(
    async (opts: {
      out: string;
      root: string;
      role?: string;
      since?: string;
      format: "alpaca" | "chat";
      reviewDecision?: "approve" | "request_changes";
    }) => {
      const { exportDataset, parseSince } = await import("./runs/dataset.js");
      const { ROLE_BUCKETS } = await import("./types.js");

      let role: import("./types.js").RoleBucket | undefined;
      if (opts.role) {
        if (!(ROLE_BUCKETS as readonly string[]).includes(opts.role)) {
          console.error(
            pc.red(
              `--role must be one of ${ROLE_BUCKETS.join(" / ")}; got ${
                opts.role
              }`,
            ),
          );
          process.exit(2);
        }
        role = opts.role as import("./types.js").RoleBucket;
      }

      let since: Date | undefined;
      if (opts.since) {
        const parsed = parseSince(opts.since);
        if (parsed === null) {
          console.error(
            pc.red(
              `--since ${JSON.stringify(opts.since)}: expected YYYY-MM-DD or ISO 8601`,
            ),
          );
          process.exit(2);
        }
        since = parsed;
      }

      const stats = exportDataset(opts.root, opts.out, {
        role,
        since,
        format: opts.format,
        reviewDecision: opts.reviewDecision,
      });

      if (stats.rows === 0) {
        console.error(
          pc.yellow(
            "wrote 0 rows. CLAW_SQUAD_LOG_PROMPTS=1 must be set *before* a run for that run's IO to be exported.",
          ),
        );
      }
      const note = stats.skippedNoIo
        ? ` (${stats.skippedNoIo} event(s) skipped: missing prompt/response)`
        : "";
      console.log(`wrote ${stats.rows} row(s) to ${stats.outputPath}${note}`);
    },
  );


program
  .command("mcp")
  .description(
    "Run claw-squad as an MCP server over stdio. Lets Claude Code (or any MCP client) call claw-squad's read-only tools (dashboard / runs list+purge) directly.",
  )
  .action(async () => {
    // Lazy-import: keeps the SDK + its transitive deps off the
    // cold-start cost of every other subcommand. Mirrors the
    // OTel pattern in src/tracing.ts.
    const { runMcpServer } = await import("./mcp/server.js");
    await runMcpServer();
  });

// --- worker daemon (W6.1) -------------------------------------------

const workerCmd = program
  .command("worker")
  .description(
    "Run the claw-squad worker daemon. Polls Redis for queued jobs and " +
    "executes runOrchestrator for each one. Use REDIS_URL env or --redis-url " +
    "to point at your Redis instance.",
  );

workerCmd
  .command("start")
  .description("Start the worker daemon (long-running).")
  .option(
    "--worker-id <id>",
    "Unique worker id (auto-generated if unset).",
  )
  .option(
    "--redis-url <url>",
    "Redis URL (default: REDIS_URL env or redis://localhost:6379).",
  )
  .option(
    "--poll-interval <seconds>",
    "How long to sleep between empty-queue polls (default: 1).",
    "1",
  )
  .action(async (opts: Record<string, unknown>) => {
    const { runWorkerDaemon } = await import("./daemon.js");
    console.log(pc.cyan("claw-squad worker daemon starting…"));
    console.log(pc.dim("  Ctrl-C or SIGTERM to stop gracefully."));
    try {
      await runWorkerDaemon({
        workerId: opts.workerId as string | undefined,
        redisUrl: opts.redisUrl as string | undefined,
        pollIntervalS: Number(opts.pollInterval ?? 1),
      });
    } catch (err) {
      console.error(pc.red(`worker failed: ${(err as Error).message}`));
      process.exit(1);
    }
  });

workerCmd
  .command("once")
  .description("Drain all currently-queued jobs and exit.")
  .option(
    "--redis-url <url>",
    "Redis URL (default: REDIS_URL env or redis://localhost:6379).",
  )
  .option("--max-jobs <n>", "Max jobs to drain (default: 1000).", "1000")
  .action(async (opts: Record<string, unknown>) => {
    const { drainQueueOnce } = await import("./daemon.js");
    const n = await drainQueueOnce({
      redisUrl: opts.redisUrl as string | undefined,
      maxJobs: Number(opts.maxJobs ?? 1000),
    });
    console.log(pc.green(`drained ${n} job(s)`));
  });

workerCmd
  .command("submit")
  .description("Enqueue a new job (does not run it; use 'worker start' to process it).")
  .argument("<requirement>", "the feature / task / question to work on")
  .requiredOption("--root <path>", "repo root")
  .option("--github", "enable GitHub integration")
  .option("--github-repo <owner/repo>", "GitHub repo")
  .option("--max-loops <n>", "max tasks per run", "10")
  .option("--max-review-rounds <n>", "max review rounds per task", "3")
  .option("--hooks <path>", "path to hooks module")
  .action(async (requirement: string, opts: Record<string, unknown>) => {
    const { submitJob } = await import("./daemon.js");
    const { loadAgentConfig, loadReposFromFile } = await import("./config.js");
    const agentConfig = loadAgentConfig({ repoRoot: String(opts.root) });
    const repos = loadReposFromFile({ repoRoot: String(opts.root) });
    const runOptions: Record<string, unknown> = {
      githubEnabled: opts.github === true,
      githubRepo: opts.githubRepo as string | undefined,
      maxLoops: Number(opts.maxLoops ?? 10),
      maxReviewRounds: Number(opts.maxReviewRounds ?? 3),
      repos: repos as RunConfig["repos"] | undefined,
    };
    if (opts.hooks) runOptions.hooksPath = opts.hooks;
    const job = await submitJob({
      requirement,
      repoRoot: String(opts.root),
      agentConfig,
      options: runOptions as Partial<RunConfig>,
    });
    console.log(pc.green(`enqueued job ${job.id}`));
    console.log(pc.dim(`  repoRoot: ${job.repoRoot}`));
  });

workerCmd
  .command("status")
  .description("Show worker and queue status (Redis).")
  .option(
    "--redis-url <url>",
    "Redis URL (default: REDIS_URL env or redis://localhost:6379).",
  )
  .action(async (opts: Record<string, unknown>) => {
    const { queueDepth, listActiveWorkers } = await import("./queue.js");
    const { setRedisFactory } = await import("./queue.js");
    if (opts.redisUrl) {
      setRedisFactory(async () => {
        const { createClient } = await import("redis");
        const client = createClient({ url: opts.redisUrl as string });
        await client.connect();
        return client as unknown as import("./queue.js").RedisClient;
      });
    }
    const depth = await queueDepth();
    const workers = await listActiveWorkers();
    console.log(pc.bold("claw-squad queue"));
    console.log(`  pending:     ${pc.cyan(String(depth.pending))}`);
    console.log(`  processing:  ${pc.cyan(String(depth.processing))}`);
    console.log(`  dead-letter:  ${pc.yellow(String(depth.dead))}`);
    console.log(pc.bold("workers"));
    if (workers.length === 0) {
      console.log(pc.dim("  (no active workers)"));
    } else {
      for (const w of workers) {
        console.log(
          `  ${pc.green(w.id)}  started=${w.startedAt}  last-heartbeat=${w.lastHeartbeat}${w.currentJobId ? `  current-job=${w.currentJobId}` : ""}`,
        );
      }
    }
  });

workerCmd
  .command("requeue-dead")
  .description("Move dead-letter jobs back to the pending queue (resets retry count).")
  .option(
    "--redis-url <url>",
    "Redis URL (default: REDIS_URL env or redis://localhost:6379).",
  )
  .option("--max <n>", "Max jobs to requeue (default: 100).", "100")
  .action(async (opts: Record<string, unknown>) => {
    const { requeueDeadJobs } = await import("./queue.js");
    const { setRedisFactory } = await import("./queue.js");
    if (opts.redisUrl) {
      setRedisFactory(async () => {
        const { createClient } = await import("redis");
        const client = createClient({ url: opts.redisUrl as string });
        await client.connect();
        return client as unknown as import("./queue.js").RedisClient;
      });
    }
    const n = await requeueDeadJobs(Number(opts.max ?? 100));
    console.log(pc.green(`requeued ${n} dead-letter job(s)`));
  });

program.parseAsync().catch((err) => {
  console.error(pc.red((err as Error).message));
  process.exit(1);
});
