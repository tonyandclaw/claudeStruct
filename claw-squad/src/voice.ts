/**
 * Voice capture + transcription (W11.3).
 *
 * The Python `cs voice transcribe` already wraps faster-whisper +
 * mic capture. Rather than ship a Node whisper.cpp binding here
 * (heavy native dep, separate model download), claw-squad
 * delegates to `cs voice transcribe` over stdio. The Python side
 * already produces plain text on stdout; capturing it is the whole
 * integration.
 *
 * Why delegation, not a Node binding?
 *
 * - Single source of truth for the model + language config; users
 *   don't have to keep two voice setups in sync.
 * - The user already has `cs` installed (claw-squad's docs assume
 *   a co-installed claudestruct for cross-tool work).
 * - A native whisper.cpp binding would balloon the install — N₂
 *   audio libs and a ~150 MB model on first run.
 *
 * If the operator hasn't installed `cs` (or `cs[voice]`) the
 * delegate fails with a clear, actionable error pointing at the
 * install command. Tests stub the spawn surface so no real audio
 * device is ever touched.
 */
import { spawnSync, type SpawnSyncReturns } from "node:child_process";


export interface VoiceOptions {
  /** Whisper model id passed to `cs voice transcribe --model`. */
  model?: string;
  /** Optional language hint (`en` / `zh` / etc.). */
  language?: string;
  /** Recording duration in seconds. */
  seconds?: number;
  /** Audio device index (sounddevice). */
  device?: number;
  /**
   * Override the `cs` binary path. Defaults to `"cs"` (relies on
   * PATH). Tests inject a stub script.
   */
  csBinary?: string;
}


export class VoiceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VoiceError";
  }
}


/** Spawn signature kept narrow for test injection. */
type SpawnFn = typeof spawnSync;


/**
 * Run `cs voice transcribe` and return its stdout.
 *
 * Failure modes (each maps to a distinct VoiceError message so the
 * caller can surface a useful hint):
 *
 * - cs binary not found (ENOENT) — install hint
 * - cs ran but exited non-zero — propagate stderr (transient mic
 *   error, missing `[voice]` extra, etc.)
 * - empty stdout — caller treats as "no speech detected"
 */
export function transcribeViaCs(
  opts: VoiceOptions = {},
  spawn: SpawnFn = spawnSync,
): string {
  const csBinary = opts.csBinary ?? "cs";
  const args = ["voice", "transcribe"];
  if (opts.model) args.push("--model", opts.model);
  if (opts.language) args.push("--language", opts.language);
  if (opts.seconds !== undefined) {
    args.push("--seconds", String(opts.seconds));
  }
  if (opts.device !== undefined) {
    args.push("--device", String(opts.device));
  }

  let res: SpawnSyncReturns<string>;
  try {
    res = spawn(csBinary, args, {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (err) {
    throw new VoiceError(
      `failed to spawn ${csBinary}: ${(err as Error).message}`,
    );
  }
  if (res.error && (res.error as NodeJS.ErrnoException).code === "ENOENT") {
    throw new VoiceError(
      `${csBinary} not found on PATH. Install with ` +
      `\`pip install 'claudestruct[voice]'\` and try again.`,
    );
  }
  if (res.status !== 0) {
    const stderr = (res.stderr ?? "").trim();
    throw new VoiceError(
      `${csBinary} voice transcribe exited ${res.status ?? "<no status>"}: ` +
      (stderr || "<no stderr>"),
    );
  }
  return (res.stdout ?? "").trim();
}
