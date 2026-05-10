import { describe, expect, it, vi } from "vitest";

import { transcribeViaCs, VoiceError } from "../src/voice.js";


/** Minimal `spawnSync`-shaped stub for tests. */
function fakeSpawn(
  result: {
    stdout?: string;
    stderr?: string;
    status?: number | null;
    error?: NodeJS.ErrnoException | null;
  },
): typeof import("node:child_process").spawnSync {
  // The real type is overloaded; cast to any at the boundary so
  // callers see the real shape.
  return ((cmd: string, args: ReadonlyArray<string>) => {
    return {
      pid: 1234,
      output: ["", result.stdout ?? "", result.stderr ?? ""],
      stdout: result.stdout ?? "",
      stderr: result.stderr ?? "",
      status: result.status ?? 0,
      signal: null,
      error: result.error ?? undefined,
    } as ReturnType<typeof import("node:child_process").spawnSync>;
  }) as typeof import("node:child_process").spawnSync;
}


describe("transcribeViaCs", () => {
  it("returns trimmed stdout from a successful cs voice transcribe", () => {
    const out = transcribeViaCs(
      {},
      fakeSpawn({ stdout: "  hello world  \n", status: 0 }),
    );
    expect(out).toBe("hello world");
  });

  it("threads --model / --language / --seconds / --device through", () => {
    const seen: { cmd: string; args: ReadonlyArray<string> }[] = [];
    const spawn = ((cmd: string, args: ReadonlyArray<string>) => {
      seen.push({ cmd, args });
      return {
        stdout: "ok",
        stderr: "",
        status: 0,
        signal: null,
        pid: 1,
        output: [],
        error: undefined,
      } as ReturnType<typeof import("node:child_process").spawnSync>;
    }) as typeof import("node:child_process").spawnSync;

    transcribeViaCs(
      { model: "base.en", language: "en", seconds: 5, device: 0 },
      spawn,
    );
    expect(seen.length).toBe(1);
    expect(seen[0].cmd).toBe("cs");
    expect(seen[0].args).toEqual([
      "voice", "transcribe",
      "--model", "base.en",
      "--language", "en",
      "--seconds", "5",
      "--device", "0",
    ]);
  });

  it("respects csBinary override (for tests / non-PATH installs)", () => {
    const seen: string[] = [];
    const spawn = ((cmd: string) => {
      seen.push(cmd);
      return {
        stdout: "x", stderr: "", status: 0, signal: null,
        pid: 1, output: [], error: undefined,
      } as ReturnType<typeof import("node:child_process").spawnSync>;
    }) as typeof import("node:child_process").spawnSync;
    transcribeViaCs({ csBinary: "/opt/bin/cs" }, spawn);
    expect(seen).toEqual(["/opt/bin/cs"]);
  });

  it("ENOENT (cs not installed) surfaces a VoiceError with install hint", () => {
    const enoent = new Error("not found") as NodeJS.ErrnoException;
    enoent.code = "ENOENT";
    expect(() =>
      transcribeViaCs({}, fakeSpawn({ error: enoent, status: null })),
    ).toThrow(/Install with `pip install 'claudestruct\[voice\]'`/);
  });

  it("non-zero exit propagates stderr in a VoiceError", () => {
    expect(() =>
      transcribeViaCs(
        {},
        fakeSpawn({ status: 2, stderr: "audio device busy" }),
      ),
    ).toThrow(/exited 2.*audio device busy/);
  });

  it("non-zero exit with empty stderr still raises something readable", () => {
    expect(() =>
      transcribeViaCs({}, fakeSpawn({ status: 137, stderr: "" })),
    ).toThrow(VoiceError);
  });

  it("empty stdout returns empty string (caller decides what to do)", () => {
    expect(
      transcribeViaCs({}, fakeSpawn({ stdout: "", status: 0 })),
    ).toBe("");
  });

  it("uses default model when --model is not passed", () => {
    let captured: ReadonlyArray<string> = [];
    const spawn = ((_cmd: string, args: ReadonlyArray<string>) => {
      captured = args;
      return {
        stdout: "ok", stderr: "", status: 0, signal: null,
        pid: 1, output: [], error: undefined,
      } as ReturnType<typeof import("node:child_process").spawnSync>;
    }) as typeof import("node:child_process").spawnSync;
    transcribeViaCs({}, spawn);
    expect(captured).toEqual(["voice", "transcribe"]);
  });
});
