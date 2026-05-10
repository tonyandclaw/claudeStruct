/**
 * Dataset export for fine-tuning (W10.6 — TS side).
 *
 * Mirrors `src/claudestruct/dataset.py`. Walks the per-run JSONL log
 * directory (`<repoRoot>/.claw-squad/runs/*.jsonl`), pulls the
 * opt-in `run-io` events out, converts them to a training-ready
 * shape (alpaca or chat format), and writes JSONL.
 *
 * The capture itself is opt-in via `CLAW_SQUAD_LOG_PROMPTS=1` (see
 * `runIoEnabled()` below). Without it, the run logs contain no IO
 * events and the exporter writes a well-formed empty file.
 *
 * Two output formats:
 *  - `alpaca` (default) — `{instruction, input, output}` triples,
 *    consumed natively by axolotl / unsloth / TRL.
 *  - `chat` — `{messages: [...]}`. Better fit for chat-tuned bases
 *    (Qwen-Coder-Instruct, Llama-3-Instruct).
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import type { RoleBucket } from "../types.js";
import type { RunLogEvent } from "./log.js";

// 100 KB cap on captured response text — same as the Python side.
// Bigger responses are silent log bloat; the cap is high enough that
// real review verdicts fit but a runaway model can't fill the disk.
export const RUN_IO_RESPONSE_CAP = 100 * 1024;

/**
 * Privacy default-off. Set CLAW_SQUAD_LOG_PROMPTS=1 (or true / yes /
 * on) to make the orchestrator emit `run-io` events alongside the
 * usual usage / phase metadata.
 */
export function runIoEnabled(): boolean {
  const v = (process.env.CLAW_SQUAD_LOG_PROMPTS ?? "").trim().toLowerCase();
  return v === "1" || v === "true" || v === "yes" || v === "on";
}

/**
 * Build a `run-io` event from raw inputs. Hoisted so the orchestrator
 * (when we wire it) and tests share the truncation logic.
 */
export function makeRunIoEvent(args: {
  role: RoleBucket;
  subagentName?: string;
  prompt: string;
  responseText: string;
}): Extract<RunLogEvent, { type: "run-io" }> {
  const truncated = args.responseText.length > RUN_IO_RESPONSE_CAP;
  return {
    type: "run-io",
    ts: new Date().toISOString(),
    role: args.role,
    ...(args.subagentName ? { subagentName: args.subagentName } : {}),
    prompt: args.prompt,
    responseText: truncated
      ? args.responseText.slice(0, RUN_IO_RESPONSE_CAP)
      : args.responseText,
    responseTruncated: truncated,
  };
}

/**
 * Emit a `run-io` event when CLAW_SQUAD_LOG_PROMPTS is enabled and a
 * runLog handle is available. The agent layer's hot path calls this
 * unconditionally after each provider.invoke; the gate lives here so
 * the call site stays a one-liner.
 *
 * Decoupled from `appendEvent` import to avoid a circular dep —
 * callers pass the appender they already have.
 */
export function emitRunIoIfEnabled(
  appender: (event: Extract<RunLogEvent, { type: "run-io" }>) => void,
  args: {
    runLog?: { path: string } | null | undefined;
    role: RoleBucket;
    subagentName?: string;
    prompt: string;
    responseText: string;
  },
): void {
  if (!args.runLog || !runIoEnabled()) return;
  appender(
    makeRunIoEvent({
      role: args.role,
      subagentName: args.subagentName,
      prompt: args.prompt,
      responseText: args.responseText,
    }),
  );
}

// --- Walking -------------------------------------------------------

export interface WalkFilters {
  /** Restrict to a single role bucket (e.g. only `coder` for fine-tune). */
  role?: RoleBucket;
  /** ISO 8601 cutoff. Events with no parseable `ts` are kept — better
   *  to over-include than silently drop. */
  since?: Date;
  /**
   * W11.5 — verdict-aware filter. When set, restrict to runs whose
   * `run-end` event has the matching `reason`. The orchestrator
   * only reaches `reason="complete"` when the Reviewer approved at
   * some point, so `reasons={"complete"}` is the standard
   * "include only review-approved runs" filter for fine-tuning
   * datasets where you don't want to learn from rolled-back work.
   *
   * Runs with no `run-end` event (in-flight, crashed) are
   * EXCLUDED when this filter is set — better to drop a row than
   * to silently include partial work.
   */
  reasons?: Set<string>;
}

function eventIsRunIo(
  e: RunLogEvent,
): e is Extract<RunLogEvent, { type: "run-io" }> {
  return e.type === "run-io";
}


function eventIsRunEnd(
  e: RunLogEvent,
): e is Extract<RunLogEvent, { type: "run-end" }> {
  return e.type === "run-end";
}

/**
 * Yield every `run-io` event under `<repoRoot>/.claw-squad/runs/`,
 * filtered by the given criteria. A single corrupt JSONL line is
 * skipped (loadOneRun already does this); a missing runs dir means
 * empty result, not an error.
 *
 * When `filters.reasons` is set, the file is read in two passes:
 * first to find the `run-end` event (typically the last line) and
 * decide whether to include the file at all; second to emit the
 * `run-io` rows. Most run files are small enough that the
 * double-read is cheaper than threading a "buffered" mode through
 * the existing single-pass shape.
 */
export function* walkRunIo(
  repoRoot: string,
  filters: WalkFilters = {},
): Generator<Extract<RunLogEvent, { type: "run-io" }>> {
  const dir = join(repoRoot, ".claw-squad", "runs");
  if (!existsSync(dir)) return;
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".jsonl"))
    .sort();
  for (const f of files) {
    let raw: string;
    try {
      raw = readFileSync(join(dir, f), "utf-8");
    } catch {
      continue;
    }
    // Pre-pass: when the verdict filter is active, find the
    // `run-end` reason and skip files that don't match. We
    // iterate through the file once for parsing; the IO rows are
    // collected only if the reason check passes (or no filter).
    let parsedLines: RunLogEvent[];
    if (filters.reasons) {
      parsedLines = [];
      let endReason: string | undefined;
      for (const line of raw.split("\n")) {
        if (line.length === 0) continue;
        let parsed: RunLogEvent;
        try {
          parsed = JSON.parse(line) as RunLogEvent;
        } catch {
          continue;
        }
        parsedLines.push(parsed);
        if (eventIsRunEnd(parsed)) {
          endReason = parsed.reason;
        }
      }
      if (endReason === undefined || !filters.reasons.has(endReason)) {
        // File didn't end (in-flight, crashed) or ended with a
        // non-matching reason — drop every row from it.
        continue;
      }
    } else {
      parsedLines = [];
      for (const line of raw.split("\n")) {
        if (line.length === 0) continue;
        try {
          parsedLines.push(JSON.parse(line) as RunLogEvent);
        } catch {
          continue;
        }
      }
    }
    for (const parsed of parsedLines) {
      if (!eventIsRunIo(parsed)) continue;
      if (filters.role && parsed.role !== filters.role) continue;
      if (filters.since) {
        const ts = Date.parse(parsed.ts);
        if (!Number.isNaN(ts) && ts < filters.since.getTime()) continue;
      }
      yield parsed;
    }
  }
}

// --- Format conversion --------------------------------------------

const ROLE_INSTRUCTIONS: Record<RoleBucket, string> = {
  // Short, consistent prefixes — fine-tunes work better when the
  // template is stable.
  planner: "You are a tech lead. Produce a step-by-step implementation plan.",
  coder: "You are a software engineer. Make the requested code change.",
  reviewer:
    "You are a senior reviewer. Surface bugs, security issues, and maintainability concerns.",
  subagent: "Respond to the request below.",
};

export interface AlpacaRow {
  instruction: string;
  input: string;
  output: string;
}

export interface ChatRow {
  messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
}

export function toAlpaca(
  e: Extract<RunLogEvent, { type: "run-io" }>,
): AlpacaRow | null {
  if (!e.prompt?.trim() || !e.responseText?.trim()) return null;
  return {
    instruction: ROLE_INSTRUCTIONS[e.role] ?? "Respond to the request below.",
    input: e.prompt,
    output: e.responseText,
  };
}

export function toChat(
  e: Extract<RunLogEvent, { type: "run-io" }>,
): ChatRow | null {
  if (!e.prompt?.trim() || !e.responseText?.trim()) return null;
  const sys = ROLE_INSTRUCTIONS[e.role] ?? "Respond to the request below.";
  return {
    messages: [
      { role: "system", content: sys },
      { role: "user", content: e.prompt },
      { role: "assistant", content: e.responseText },
    ],
  };
}

// --- Export driver ------------------------------------------------

export type DatasetFormat = "alpaca" | "chat";

export interface DatasetStats {
  rows: number;
  skippedNoIo: number;
  outputPath: string;
}

export interface ExportOptions extends WalkFilters {
  format?: DatasetFormat;
}

/**
 * Walk + filter + format + write. Empty result is not an error: the
 * destination file is created (truncated if it existed) so a
 * downstream `wc -l` doesn't have to special-case missing input.
 */
export function exportDataset(
  repoRoot: string,
  outputPath: string,
  options: ExportOptions = {},
): DatasetStats {
  const format: DatasetFormat = options.format ?? "alpaca";
  const formatter = format === "chat" ? toChat : toAlpaca;

  // Truncate the output up-front so an interrupted export doesn't
  // leave stale rows behind.
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, "", "utf-8");

  let rows = 0;
  let skippedNoIo = 0;
  for (const event of walkRunIo(repoRoot, options)) {
    const converted = formatter(event);
    if (converted === null) {
      skippedNoIo += 1;
      continue;
    }
    appendFileSync(outputPath, JSON.stringify(converted) + "\n", "utf-8");
    rows += 1;
  }
  return { rows, skippedNoIo, outputPath };
}

/**
 * Lenient "YYYY-MM-DD or full ISO" parser used by the CLI flag.
 * Returns null on garbage so the CLI can render its own error.
 */
export function parseSince(value: string): Date | null {
  if (!value) return null;
  // First try full ISO. If only a date is given, append T00:00:00Z so
  // Date.parse interprets in UTC — otherwise it falls back to local
  // time and the cutoff drifts by the operator's offset.
  const candidate = /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? `${value}T00:00:00Z`
    : value;
  const t = Date.parse(candidate);
  if (Number.isNaN(t)) return null;
  return new Date(t);
}
