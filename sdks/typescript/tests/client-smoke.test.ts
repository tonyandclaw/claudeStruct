import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  AuthError,
  BudgetExceededError,
  Client,
  NotFoundError,
} from "../src/index.js";


function fakeFetch(
  responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> }>,
): typeof fetch {
  let i = 0;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fn = async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: url.toString(), init });
    const r = responses[i] ?? responses[responses.length - 1];
    if (!r) throw new Error("no fake responses left");
    i++;
    return new Response(
      r.body === undefined ? null : JSON.stringify(r.body),
      {
        status: r.status,
        headers: {
          "X-CS-Api-Version": "v1",
          "Content-Type": "application/json",
          ...(r.headers ?? {}),
        },
      },
    );
  };
  (fn as any).calls = calls;
  return fn as unknown as typeof fetch;
}


describe("Client constructor", () => {
  it("requires baseUrl and apiKey", () => {
    expect(() => new Client({ baseUrl: "", apiKey: "ck_x" })).toThrow();
    expect(() => new Client({ baseUrl: "https://e", apiKey: "" })).toThrow();
  });

  it("strips trailing slashes from baseUrl", () => {
    const c = new Client({
      baseUrl: "https://e///",
      apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 200, body: {} }]),
    });
    expect(c.baseUrl).toBe("https://e");
  });

  it("exposes documented resource namespaces", () => {
    const c = new Client({
      baseUrl: "https://e",
      apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 200, body: {} }]),
    });
    expect(c.runs).toBeDefined();
    expect(c.dashboard).toBeDefined();
    expect(c.keys).toBeDefined();
    expect(c.budget).toBeDefined();
  });
});


describe("Status-code → typed-exception mapping", () => {
  it("401 → AuthError", async () => {
    const c = new Client({
      baseUrl: "https://e", apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 401, body: { detail: "bad bearer" } }]),
    });
    await expect(c.dashboard.get()).rejects.toBeInstanceOf(AuthError);
  });

  it("402 → BudgetExceededError", async () => {
    const c = new Client({
      baseUrl: "https://e", apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 402, body: { detail: "cap reached" } }]),
    });
    await expect(c.runs.create({ task: "dev", description: "x" })).rejects.toBeInstanceOf(BudgetExceededError);
  });

  it("404 → NotFoundError", async () => {
    const c = new Client({
      baseUrl: "https://e", apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 404, body: { detail: "not found" } }]),
    });
    await expect(c.runs.get("nope")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("502 → ApiError (generic 5xx)", async () => {
    const c = new Client({
      baseUrl: "https://e", apiKey: "ck_x",
      fetcher: fakeFetch([{ status: 502, body: "" }]),
    });
    await expect(c.dashboard.get()).rejects.toBeInstanceOf(ApiError);
  });
});


describe("Auth header + version drift warning", () => {
  it("attaches Authorization: Bearer ck_... to every request", async () => {
    const fake = fakeFetch([{ status: 200, body: { runs: [] } }]);
    const c = new Client({ baseUrl: "https://e", apiKey: "ck_topsecret", fetcher: fake });
    await c.dashboard.get();
    const calls = (fake as any).calls;
    expect(calls[0].init.headers.Authorization).toBe("Bearer ck_topsecret");
  });

  it("warns when server returns a different X-CS-Api-Version", async () => {
    const fake = fakeFetch([
      { status: 200, body: { runs: [] }, headers: { "X-CS-Api-Version": "v2" } },
    ]);
    const c = new Client({ baseUrl: "https://e", apiKey: "ck_x", fetcher: fake });
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    await c.dashboard.get();
    expect(warnSpy).toHaveBeenCalled();
    expect(warnSpy.mock.calls[0]?.[0]).toContain("v2");
    warnSpy.mockRestore();
  });
});


describe("Resource methods", () => {
  it("runs.create POSTs to /v1/runs and returns camelCased fields", async () => {
    const fake = fakeFetch([
      { status: 202, body: { run_id: "abc", status: "queued", note: "n" } },
    ]);
    const c = new Client({ baseUrl: "https://e", apiKey: "ck_x", fetcher: fake });
    const res = await c.runs.create({ task: "dev", description: "add retry" });
    expect(res.runId).toBe("abc");
    expect(res.status).toBe("queued");
    const calls = (fake as any).calls;
    expect(calls[0].url).toBe("https://e/v1/runs");
    expect(calls[0].init.method).toBe("POST");
  });

  it("dashboard.getTeam includes ?team=<slug> in the URL", async () => {
    const fake = fakeFetch([{
      status: 200,
      body: {
        org_id: 1, org_slug: "acme",
        total_runs: 0, total_cost_usd: 0,
        by_author: [], by_task: [], recent: [],
      },
    }]);
    const c = new Client({ baseUrl: "https://e", apiKey: "ck_x", fetcher: fake });
    await c.dashboard.getTeam({ limit: 20, team: "platform" });
    const calls = (fake as any).calls;
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe("/v1/dashboard/team");
    expect(url.searchParams.get("team")).toBe("platform");
    expect(url.searchParams.get("limit")).toBe("20");
  });

  it("keys.revoke uses DELETE", async () => {
    const fake = fakeFetch([{ status: 204 }]);
    const c = new Client({ baseUrl: "https://e", apiKey: "ck_x", fetcher: fake });
    await c.keys.revoke("key_abc");
    const calls = (fake as any).calls;
    expect(calls[0].init.method).toBe("DELETE");
    expect(calls[0].url).toBe("https://e/v1/keys/key_abc");
  });
});
