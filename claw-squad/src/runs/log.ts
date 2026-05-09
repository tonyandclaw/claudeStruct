/**
 * Per-run event log.
 *
 * Every orchestrator run writes a single JSONL file under
 * `.claw-squad/runs/<iso-timestamp>.jsonl`. Each line is one event:
 *
 *   {"type":"run-start","ts":"...","requirement":"...","config":{...}}
 *   {"type":"usage","ts":"...","role":"planner","costUsd":0.01,...}
 *   {"type":"todo-complete","ts":"...","id":"T1","iterations":2}
 *   {"type":"run-end","ts":"...","reason":"complete","totals":{...}}
 *
 * JSONL (one JSON object per line) is chosen because:
 *   - Crash-safe: an interrupted run still has a valid, tail-readable
 *     log. No "we never wrote the closing bracket" pathology.
 *   - Append-only: each event is a single fsync-able line.
 *   - Tailable: `tail -f runs/<latest>.jsonl | jq .` works today.
 *
 * The dashboard subcommand folds this stream back into a per-run
 * summary. Tests round-trip append → load → fold.
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import type { RoleBucket } from "../types.js";

export interface RunLogHandle {
  /** Absolute path to the .jsonl file (under .claw-squad/runs/). */
  path: string;
  /** When this run started (used as filename base). */
  startedAt: string;
  /**
   * Optional secondary path. When set, every event also lands here as
   * an additional JSONL line. Used by `--log-json <path>` so users can
   * pipe to their own observability stack without scanning
   * .claw-squad/runs/ for the latest file.
   */
  mirrorPath?: string;
}

export type RunLogEvent =
  | {
      type: "run-start";
      ts: string;
      requirement: string;
      /** Lightweight config snapshot for later audit. */
      config: {
        repoRoot: string;
        githubEnabled: boolean;
        sandboxEnabled: boolean;
        maxLoops: number;
        maxReviewRounds: number;
      };
    }
  | {
      type: "usage";
      ts: string;
      role: RoleBucket;
      /** Provider name from the underlying Provider ("anthropic", "openai", ...). */
      provider: string;
      inputTokens: number;
      outputTokens: number;
      cacheReadTokens: number;
      cacheCreationTokens: number;
      costUsd: number;
      /**
       * Set when role is "subagent" and the call targeted a named
       * subagent. The dashboard uses this to attribute spend per
       * subagent. Omitted for role calls (planner/coder/reviewer).
       */
      subagentName?: string;
    }
  | {
      type: "phase";
      ts: string;
      /** Free-form phase label ("planner.clarification", "coder.round.2", ...). */
      label: string;
      message?: string;
    }
  | {
      type: "todo-complete";
      ts: string;
      id: string;
      title: string;
      iterations: number;
      rolledBack?: boolean;
    }
  | {
      type: "run-end";
      ts: string;
      reason: "complete" | "max_loops" | "blocked" | "aborted" | "dry_run";
      overall: {
        costUsd: number;
        cacheSavedUsd: number;
        calls: number;
      };
    }
  | {
      // W10.6 — opt-in IO capture for fine-tuning datasets. Off by
      // default because raw prompts often contain proprietary code +
      // credentials. Set CLAW_SQUAD_LOG_PROMPTS=1 to enable; consumed
      // by `claw-squad dataset export`. Mirrors the Python-side
      // `run.io` event in claudestruct.logging.
      type: "run-io";
      ts: string;
      role: RoleBucket;
      subagentName?: string;
      prompt: string;
      responseText: string;
      // True when the model's response exceeded the 100 KB cap and
      // got truncated before disk write.
      responseTruncated?: boolean;
      // W11.5 — only set for role="reviewer". The verdict the
      // Reviewer arrived at on this turn, mirrored from the parsed
      // ReviewVerdict.decision. Lets `dataset export
      // --review-decision approve` scope the fine-tune corpus to
      // turns that passed review without re-parsing the response.
      reviewDecision?: "approve" | "request_changes";
    };

/**
 * Open a run log. Creates the runs directory if missing and returns a
 * handle with the file path. First write to the file happens on the
 * first appendEvent call — we don't touch disk until there's content.
 */
export function startRun(
  repoRoot: string,
  options: { mirrorPath?: string } = {},
): RunLogHandle {
  const dir = join(repoRoot, ".claw-squad", "runs");
  mkdirSync(dir, { recursive: true });
  const startedAt = new Date().toISOString();
  // Filesystems hate `:` in filenames — flatten to something safe.
  const slug = startedAt.replace(/[:.]/g, "-");
  const path = join(dir, `${slug}.jsonl`);
  // Pre-create the mirror's parent directory so the first append
  // doesn't fail with ENOENT. Skip silently if the path is invalid —
  // the per-event write also handles failures.
  if (options.mirrorPath) {
    try {
      mkdirSync(dirname(options.mirrorPath), { recursive: true });
    } catch {
      /* best-effort */
    }
  }
  return { path, startedAt, mirrorPath: options.mirrorPath };
}

export function appendEvent(handle: RunLogHandle, event: RunLogEvent): void {
  const line = JSON.stringify(event) + "\n";
  appendFileSync(handle.path, line, "utf-8");
  // Mirror is best-effort: a misconfigured --log-json path must never
  // abort a real run. Swallow + continue.
  if (handle.mirrorPath) {
    try {
      appendFileSync(handle.mirrorPath, line, "utf-8");
    } catch {
      /* ignore */
    }
  }
}

/**
 * Load every JSONL file under `.claw-squad/runs/` and return the
 * parsed events keyed by path. Malformed lines are skipped with a
 * warning — one bad line shouldn't hide the rest of history.
 */
/**
 * The basename (without extension) of a run log file. We use ISO
 * timestamps with `:` and `.` flattened to `-`, so a runId is both
 * URL-safe and chronologically sortable.
 */
export function runIdFromPath(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base.replace(/\.jsonl$/, "");
}

export function pathFromRunId(repoRoot: string, runId: string): string {
  return join(repoRoot, ".claw-squad", "runs", `${runId}.jsonl`);
}

export function loadAllRuns(
  repoRoot: string,
  log?: (msg: string) => void,
): Array<{ path: string; events: RunLogEvent[] }> {
  const dir = join(repoRoot, ".claw-squad", "runs");
  if (!existsSync(dir)) return [];
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".jsonl"))
    .sort(); // ISO-timestamped filenames sort chronologically.
  return files.map((f) => ({
    path: join(dir, f),
    events: loadOneRun(join(dir, f), log),
  }));
}

export function loadOneRun(
  path: string,
  log?: (msg: string) => void,
): RunLogEvent[] {
  const raw = readFileSync(path, "utf-8");
  const out: RunLogEvent[] = [];
  const lines = raw.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    if (line.length === 0) continue;
    try {
      out.push(JSON.parse(line) as RunLogEvent);
    } catch {
      log?.(`skipping malformed line ${i + 1} in ${path}`);
    }
  }
  return out;
}
