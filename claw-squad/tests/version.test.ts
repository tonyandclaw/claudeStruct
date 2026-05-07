import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { VERSION, packageVersion } from "../src/version.js";

describe("VERSION", () => {
  it("matches package.json so CLI and MCP metadata do not drift", () => {
    const pkg = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
    ) as { version: string };

    expect(VERSION).toBe(pkg.version);
    expect(packageVersion()).toBe(pkg.version);
  });
});
