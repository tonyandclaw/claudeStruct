/**
 * Tests for `runs/dataset.ts` (W10.6 — TS-side fine-tune export).
 *
 * Synthetic JSONL files are dropped under a tmpdir; tests never spin
 * up the orchestrator. The walker contract lives here, not the
 * orchestrator integration — that lands separately when run-io
 * emission gets wired into the agent loop.
 */

import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  exportDataset,
  makeRunIoEvent,
  parseSince,
  RUN_IO_RESPONSE_CAP,
  runIoEnabled,
  toAlpaca,
  toChat,
  walkRunIo,
} from "../src/runs/dataset.js";
import type { RunLogEvent } from "../src/runs/log.js";

function writeRunFile(
  root: string,
  name: string,
  events: RunLogEvent[],
): void {
  const dir = join(root, ".claw-squad", "runs");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, name),
    events.map((e) => JSON.stringify(e)).join("\n") + "\n",
    "utf-8",
  );
}

function ioEvent(
  overrides: Partial<Extract<RunLogEvent, { type: "run-io" }>> = {},
): Extract<RunLogEvent, { type: "run-io" }> {
  return {
    type: "run-io",
    ts: "2026-05-01T10:00:00.000Z",
    role: "reviewer",
    prompt: "look at this diff",
    responseText: "ship it",
    responseTruncated: false,
    ...overrides,
  };
}

describe("makeRunIoEvent", () => {
  it("populates fields and stamps responseTruncated=false on small responses", () => {
    const e = makeRunIoEvent({
      role: "coder",
      prompt: "add a feature",
      responseText: "patch",
    });
    expect(e.type).toBe("run-io");
    expect(e.role).toBe("coder");
    expect(e.prompt).toBe("add a feature");
    expect(e.responseText).toBe("patch");
    expect(e.responseTruncated).toBe(false);
    // ts must be valid ISO so dataset.parseSince can compare.
    expect(() => new Date(e.ts).toISOString()).not.toThrow();
  });

  it("truncates oversized responses at the 100 KB cap", () => {
    const huge = "x".repeat(RUN_IO_RESPONSE_CAP * 2);
    const e = makeRunIoEvent({
      role: "reviewer",
      prompt: "p",
      responseText: huge,
    });
    expect(e.responseText.length).toBe(RUN_IO_RESPONSE_CAP);
    expect(e.responseTruncated).toBe(true);
  });

  it("includes subagentName only when provided", () => {
    const without = makeRunIoEvent({
      role: "coder",
      prompt: "p",
      responseText: "r",
    });
    expect("subagentName" in without).toBe(false);

    const withSub = makeRunIoEvent({
      role: "subagent",
      subagentName: "research-helper",
      prompt: "p",
      responseText: "r",
    });
    expect(withSub.subagentName).toBe("research-helper");
  });

  it("includes reviewDecision only when provided (W11.5)", () => {
    const without = makeRunIoEvent({
      role: "coder",
      prompt: "p",
      responseText: "r",
    });
    expect("reviewDecision" in without).toBe(false);

    const approved = makeRunIoEvent({
      role: "reviewer",
      prompt: "p",
      responseText: "r",
      reviewDecision: "approve",
    });
    expect(approved.reviewDecision).toBe("approve");
  });
});

describe("runIoEnabled", () => {
  const original = process.env.CLAW_SQUAD_LOG_PROMPTS;
  afterEach(() => {
    if (original === undefined) delete process.env.CLAW_SQUAD_LOG_PROMPTS;
    else process.env.CLAW_SQUAD_LOG_PROMPTS = original;
  });

  it("default off when env unset", () => {
    delete process.env.CLAW_SQUAD_LOG_PROMPTS;
    expect(runIoEnabled()).toBe(false);
  });

  it.each(["1", "true", "TRUE", "yes", "on"])("truthy: %s", (v) => {
    process.env.CLAW_SQUAD_LOG_PROMPTS = v;
    expect(runIoEnabled()).toBe(true);
  });

  it.each(["0", "false", "no", "off", ""])("falsy: %s", (v) => {
    process.env.CLAW_SQUAD_LOG_PROMPTS = v;
    expect(runIoEnabled()).toBe(false);
  });
});

describe("walkRunIo", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "claw-dataset-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("yields only run-io events, skipping run-start/usage/run-end", () => {
    writeRunFile(root, "a.jsonl", [
      {
        type: "run-start",
        ts: "2026-05-01T10:00:00Z",
        requirement: "x",
        config: {
          repoRoot: root,
          githubEnabled: false,
          sandboxEnabled: false,
          maxLoops: 5,
          maxReviewRounds: 2,
        },
      },
      ioEvent({ prompt: "captured" }),
      {
        type: "usage",
        ts: "2026-05-01T10:00:01Z",
        role: "reviewer",
        provider: "anthropic",
        inputTokens: 1,
        outputTokens: 1,
        cacheReadTokens: 0,
        cacheCreationTokens: 0,
        costUsd: 0.001,
      },
    ]);
    const events = [...walkRunIo(root)];
    expect(events.length).toBe(1);
    expect(events[0].prompt).toBe("captured");
  });

  it("filters by role", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "reviewer", prompt: "rv" }),
      ioEvent({ role: "coder", prompt: "cd" }),
      ioEvent({ role: "planner", prompt: "pl" }),
    ]);
    const out = [...walkRunIo(root, { role: "coder" })].map((e) => e.prompt);
    expect(out).toEqual(["cd"]);
  });

  it("filters by since", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ ts: "2025-12-31T23:59:59Z", prompt: "old" }),
      ioEvent({ ts: "2026-05-01T10:00:00Z", prompt: "new" }),
    ]);
    const cutoff = new Date("2026-01-01T00:00:00Z");
    const out = [...walkRunIo(root, { since: cutoff })].map((e) => e.prompt);
    expect(out).toEqual(["new"]);
  });

  it("keeps events with unparseable ts (over-include over silent drop)", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ ts: "not a real timestamp", prompt: "kept" }),
    ]);
    const out = [
      ...walkRunIo(root, { since: new Date("2026-01-01T00:00:00Z") }),
    ];
    expect(out.length).toBe(1);
  });

  it("skips corrupt JSONL lines without crashing the walk", () => {
    const dir = join(root, ".claw-squad", "runs");
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, "a.jsonl"),
      [
        JSON.stringify(ioEvent({ prompt: "good1" })),
        "{not-json,broken}",
        JSON.stringify(ioEvent({ prompt: "good2" })),
      ].join("\n") + "\n",
      "utf-8",
    );
    const out = [...walkRunIo(root)].map((e) => e.prompt);
    expect(out).toEqual(["good1", "good2"]);
  });

  it("returns empty when the runs directory doesn't exist", () => {
    const empty = mkdtempSync(join(tmpdir(), "claw-empty-"));
    expect([...walkRunIo(empty)]).toEqual([]);
    rmSync(empty, { recursive: true, force: true });
  });

  it("walks multiple JSONL files in sorted (chronological) order", () => {
    writeRunFile(root, "a.jsonl", [ioEvent({ prompt: "from-a" })]);
    writeRunFile(root, "b.jsonl", [ioEvent({ prompt: "from-b" })]);
    const out = [...walkRunIo(root)].map((e) => e.prompt);
    expect(out).toEqual(["from-a", "from-b"]);
  });

  // --- W11.5 review-decision filter ---------------------------------

  it("review-decision approve: drops a whole run when no reviewer event matches", () => {
    // Run A: reviewer requested changes — coder turn must NOT survive
    // the filter even though it has nothing to do with the verdict
    // directly; the gate is per-run.
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "coder-a" }),
      ioEvent({
        role: "reviewer",
        prompt: "review-a",
        reviewDecision: "request_changes",
      }),
    ]);
    // Run B: reviewer approved.
    writeRunFile(root, "b.jsonl", [
      ioEvent({ role: "coder", prompt: "coder-b" }),
      ioEvent({
        role: "reviewer",
        prompt: "review-b",
        reviewDecision: "approve",
      }),
    ]);
    const out = [
      ...walkRunIo(root, { reviewDecision: "approve" }),
    ].map((e) => e.prompt);
    expect(out).toEqual(["coder-b", "review-b"]);
  });

  it("review-decision approve: keeps every event in a run with at least one matching reviewer event", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "c1" }),
      ioEvent({
        role: "reviewer",
        prompt: "r1",
        reviewDecision: "request_changes",
      }),
      ioEvent({ role: "coder", prompt: "c2" }),
      ioEvent({
        role: "reviewer",
        prompt: "r2",
        reviewDecision: "approve",
      }),
    ]);
    const out = [
      ...walkRunIo(root, { reviewDecision: "approve" }),
    ].map((e) => e.prompt);
    expect(out).toEqual(["c1", "r1", "c2", "r2"]);
  });

  it("review-decision drops legacy runs with no reviewDecision field", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "old-coder" }),
      ioEvent({ role: "reviewer", prompt: "old-reviewer" }), // no reviewDecision
    ]);
    const out = [...walkRunIo(root, { reviewDecision: "approve" })];
    expect(out).toEqual([]);
  });

  it("review-decision composes with role filter — reviewer turns dropped, coder turns survive", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "coder-a" }),
      ioEvent({
        role: "reviewer",
        prompt: "review-a",
        reviewDecision: "approve",
      }),
    ]);
    const out = [
      ...walkRunIo(root, {
        reviewDecision: "approve",
        role: "coder",
      }),
    ].map((e) => e.prompt);
    expect(out).toEqual(["coder-a"]);
  });

  it("review-decision request_changes is selectable too (not just approve)", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "ca" }),
      ioEvent({
        role: "reviewer",
        prompt: "ra",
        reviewDecision: "request_changes",
      }),
    ]);
    writeRunFile(root, "b.jsonl", [
      ioEvent({
        role: "reviewer",
        prompt: "rb",
        reviewDecision: "approve",
      }),
    ]);
    const out = [
      ...walkRunIo(root, { reviewDecision: "request_changes" }),
    ].map((e) => e.prompt);
    expect(out).toEqual(["ca", "ra"]);
  });
});

describe("toAlpaca / toChat", () => {
  it("toAlpaca picks role-specific instruction", () => {
    const row = toAlpaca(ioEvent({ role: "reviewer" }));
    expect(row).not.toBeNull();
    expect(row!.instruction.toLowerCase()).toContain("reviewer");
    expect(row!.input).toBe("look at this diff");
    expect(row!.output).toBe("ship it");
  });

  it("toAlpaca returns null on blank prompt or response", () => {
    expect(toAlpaca(ioEvent({ prompt: "" }))).toBeNull();
    expect(toAlpaca(ioEvent({ responseText: "   \n" }))).toBeNull();
  });

  it("toChat emits three role messages in order", () => {
    const row = toChat(ioEvent({ role: "coder" }));
    expect(row).not.toBeNull();
    const roles = row!.messages.map((m) => m.role);
    expect(roles).toEqual(["system", "user", "assistant"]);
    expect(row!.messages[1].content).toBe("look at this diff");
    expect(row!.messages[2].content).toBe("ship it");
  });
});

describe("exportDataset", () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "claw-export-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("round-trips IO events into alpaca JSONL", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ prompt: "p1", responseText: "r1" }),
      ioEvent({ prompt: "p2", responseText: "r2" }),
    ]);
    const out = join(root, "out.jsonl");
    const stats = exportDataset(root, out);
    expect(stats.rows).toBe(2);
    expect(stats.outputPath).toBe(out);
    const lines = readFileSync(out, "utf-8").split("\n").filter(Boolean);
    expect(lines.length).toBe(2);
    const first = JSON.parse(lines[0]);
    expect(first.input).toBe("p1");
    expect(first.output).toBe("r1");
  });

  it("chat format produces 3-message rows", () => {
    writeRunFile(root, "a.jsonl", [ioEvent()]);
    const out = join(root, "out.jsonl");
    exportDataset(root, out, { format: "chat" });
    const row = JSON.parse(readFileSync(out, "utf-8").trim());
    expect(row.messages.length).toBe(3);
  });

  it("creates a well-formed empty file when no IO events exist", () => {
    const out = join(root, "out.jsonl");
    const stats = exportDataset(root, out);
    expect(stats.rows).toBe(0);
    expect(readFileSync(out, "utf-8")).toBe("");
  });

  it("counts events with missing prompt/response in skippedNoIo", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ prompt: "p", responseText: "r" }),
      ioEvent({ prompt: "", responseText: "r" }),
    ]);
    const stats = exportDataset(root, join(root, "out.jsonl"));
    expect(stats.rows).toBe(1);
    expect(stats.skippedNoIo).toBe(1);
  });

  it("creates parent directories of --out", () => {
    writeRunFile(root, "a.jsonl", [ioEvent()]);
    const out = join(root, "deep", "nested", "out.jsonl");
    exportDataset(root, out);
    expect(readFileSync(out, "utf-8").length).toBeGreaterThan(0);
  });

  it("truncates the output up-front so an interrupted prior export can't bleed through", () => {
    writeRunFile(root, "a.jsonl", [ioEvent()]);
    const out = join(root, "out.jsonl");
    writeFileSync(out, "STALE\nCONTENT\n", "utf-8");
    const stats = exportDataset(root, out);
    expect(stats.rows).toBe(1);
    const text = readFileSync(out, "utf-8");
    expect(text).not.toContain("STALE");
  });

  it("--review-decision filter scopes exported rows to runs that passed review (W11.5)", () => {
    writeRunFile(root, "a.jsonl", [
      ioEvent({ role: "coder", prompt: "rejected-coder" }),
      ioEvent({
        role: "reviewer",
        prompt: "rejected-reviewer",
        reviewDecision: "request_changes",
      }),
    ]);
    writeRunFile(root, "b.jsonl", [
      ioEvent({ role: "coder", prompt: "approved-coder" }),
      ioEvent({
        role: "reviewer",
        prompt: "approved-reviewer",
        reviewDecision: "approve",
      }),
    ]);
    const out = join(root, "out.jsonl");
    const stats = exportDataset(root, out, { reviewDecision: "approve" });
    expect(stats.rows).toBe(2);
    const inputs = readFileSync(out, "utf-8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l).input as string);
    expect(inputs).toEqual(["approved-coder", "approved-reviewer"]);
  });
});

describe("parseSince", () => {
  it("accepts YYYY-MM-DD and treats it as UTC midnight", () => {
    const d = parseSince("2026-01-15");
    expect(d).not.toBeNull();
    expect(d!.toISOString()).toBe("2026-01-15T00:00:00.000Z");
  });

  it("accepts full ISO 8601", () => {
    const d = parseSince("2026-01-15T10:00:00Z");
    expect(d!.getUTCHours()).toBe(10);
  });

  it("returns null on garbage", () => {
    expect(parseSince("not a date")).toBeNull();
    expect(parseSince("")).toBeNull();
  });
});

// --- Agent integration: run-io emission ----------------------------
//
// Locks in the W10.6 wiring through to a real agent: when the env gate
// is on AND a runLog is provided, runReviewer emits a `run-io` event;
// when either is missing, it doesn't. Without these tests, a future
// refactor could silently drop the emit and the dataset exporter would
// just produce empty files.

describe("agent emits run-io when CLAW_SQUAD_LOG_PROMPTS=1", () => {
  let root: string;
  const originalEnv = process.env.CLAW_SQUAD_LOG_PROMPTS;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "claw-emit-"));
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
    if (originalEnv === undefined) delete process.env.CLAW_SQUAD_LOG_PROMPTS;
    else process.env.CLAW_SQUAD_LOG_PROMPTS = originalEnv;
  });

  // Minimal Provider that returns a canned response without touching
  // any real backend. The reviewer's `parseReviewerOutput` expects a
  // ```json block, so we feed one back to keep the agent happy.
  function fakeProvider() {
    return {
      name: "anthropic" as const,
      invoke: async () => ({
        text:
          'Looks good. ```json\n{"decision":"approve","summary":"ok","findings":[]}\n```',
        inputTokens: 1,
        outputTokens: 1,
        cacheReadTokens: 0,
        cacheCreationTokens: 0,
        costUsd: 0,
      }),
    };
  }

  it("emits run-io when env gate is on + runLog is provided", async () => {
    process.env.CLAW_SQUAD_LOG_PROMPTS = "1";
    const { startRun, loadOneRun } = await import("../src/runs/log.js");
    const { runReviewer } = await import("../src/agents/reviewer.js");

    const runLog = startRun(root);
    await runReviewer({
      todo: { id: "T1", title: "t", description: "d", status: "pending", iterations: 0 },
      diff: "diff --git a/x b/x\n",
      provider: fakeProvider(),
      runLog,
    });

    const events = loadOneRun(runLog.path);
    const ioEvents = events.filter((e) => e.type === "run-io");
    expect(ioEvents.length).toBe(1);
    expect((ioEvents[0] as { type: "run-io"; role: string }).role).toBe(
      "reviewer",
    );
    // W11.5 — reviewer must stamp its decision onto the run-io event
    // so dataset filters can scope to runs that passed review without
    // re-parsing the response text.
    expect(
      (
        ioEvents[0] as {
          type: "run-io";
          reviewDecision?: "approve" | "request_changes";
        }
      ).reviewDecision,
    ).toBe("approve");
  });

  it("does not emit run-io when env gate is off (default)", async () => {
    delete process.env.CLAW_SQUAD_LOG_PROMPTS;
    const { startRun, loadOneRun } = await import("../src/runs/log.js");
    const { runReviewer } = await import("../src/agents/reviewer.js");

    const runLog = startRun(root);
    await runReviewer({
      todo: { id: "T1", title: "t", description: "d", status: "pending", iterations: 0 },
      diff: "",
      provider: fakeProvider(),
      runLog,
    });

    // The runLog file may not even exist (no events written), or may
    // contain unrelated events; either way, no run-io.
    let events: ReturnType<typeof loadOneRun> = [];
    try {
      events = loadOneRun(runLog.path);
    } catch {
      /* file absent => no events */
    }
    expect(events.filter((e) => e.type === "run-io").length).toBe(0);
  });

  it("does not emit run-io when env gate is on but no runLog", async () => {
    process.env.CLAW_SQUAD_LOG_PROMPTS = "1";
    const { runReviewer } = await import("../src/agents/reviewer.js");

    // Just confirm the call doesn't throw without a runLog handle —
    // the agent's runLog field is optional. A missing handle means
    // "skip the capture", not "crash the run".
    await expect(
      runReviewer({
        todo: { id: "T1", title: "t", description: "d", status: "pending", iterations: 0 },
        diff: "",
        provider: fakeProvider(),
      }),
    ).resolves.not.toThrow();
  });
});
