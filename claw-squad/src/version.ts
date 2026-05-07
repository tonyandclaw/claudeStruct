import { readFileSync } from "node:fs";

const FALLBACK_VERSION = "0.1.0";

export function packageVersion(): string {
  try {
    const raw = readFileSync(new URL("../package.json", import.meta.url), "utf-8");
    const parsed = JSON.parse(raw) as { version?: unknown };
    if (typeof parsed.version === "string" && parsed.version.trim() !== "") {
      return parsed.version;
    }
  } catch {
    // The published package and Docker image both ship package.json.
    // This fallback keeps --version and MCP startup usable in unusual
    // single-file/bundled executions where package metadata is absent.
  }
  return FALLBACK_VERSION;
}

export const VERSION = packageVersion();
