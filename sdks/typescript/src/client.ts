import {
  ApiError,
  AuthError,
  BudgetExceededError,
  ForbiddenError,
  NotFoundError,
  ServerError,
} from "./errors.js";
import type {
  CreateRunRequest,
  CreateRunResponse,
  DashboardResponse,
  KeyMetadata,
  TeamDashboardResponse,
} from "./types.js";

const DEFAULT_API_VERSION = "v1";


export interface ClientOptions {
  baseUrl: string;
  apiKey: string;
  apiVersion?: string;
  /** Inject a fetch-compatible function for tests; defaults to
   * `globalThis.fetch`. */
  fetcher?: typeof fetch;
}


/** Convert a snake_case-keyed object to camelCase 1 level deep.
 *
 * The wire format uses snake_case (matches the Python server's
 * Pydantic models); the SDK exposes camelCase to TS callers.
 * Nested objects must be converted by the caller — keeping this
 * helper one-level avoids surprising mutations in deeply-nested
 * payloads.
 */
function camelize<T>(o: Record<string, unknown>): T {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    const cc = k.replace(/_([a-z0-9])/g, (_m, c) => c.toUpperCase());
    out[cc] = v;
  }
  return out as T;
}


export class Client {
  readonly baseUrl: string;
  readonly apiKey: string;
  readonly apiVersion: string;
  readonly runs: RunsResource;
  readonly dashboard: DashboardResource;
  readonly keys: KeysResource;
  readonly budget: BudgetResource;

  private readonly _fetch: typeof fetch;

  constructor(opts: ClientOptions) {
    if (!opts.baseUrl) throw new Error("baseUrl is required");
    if (!opts.apiKey) throw new Error("apiKey is required");
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.apiVersion = opts.apiVersion ?? DEFAULT_API_VERSION;
    this._fetch = opts.fetcher ?? globalThis.fetch.bind(globalThis);
    this.runs = new RunsResource(this);
    this.dashboard = new DashboardResource(this);
    this.keys = new KeysResource(this);
    this.budget = new BudgetResource(this);
  }

  private headers(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      Accept: "application/json",
    };
  }

  private checkVersion(resp: Response): void {
    const v = resp.headers.get("X-CS-Api-Version");
    if (v && v !== this.apiVersion) {
      // eslint-disable-next-line no-console
      console.warn(
        `[claudestruct-sdk] server X-CS-Api-Version=${v} but SDK targets ` +
        `${this.apiVersion}; the response shape may have changed. ` +
        `Pin the SDK or upgrade.`,
      );
    }
  }

  private async raiseForStatus(resp: Response): Promise<void> {
    if (resp.status >= 200 && resp.status < 300) return;
    let detail: string | undefined;
    try {
      const body = await resp.clone().json();
      if (body && typeof body === "object" && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Non-JSON body — leave detail undefined.
    }
    const code = resp.status;
    if (code === 401) throw new AuthError(detail);
    if (code === 402) throw new BudgetExceededError(detail);
    if (code === 403) throw new ForbiddenError(detail);
    if (code === 404) throw new NotFoundError(detail);
    if (code >= 500) throw new ServerError(code, detail);
    throw new ApiError(code, detail);
  }

  async _get(path: string, params?: Record<string, unknown>): Promise<unknown> {
    const url = new URL(this.baseUrl + path);
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) {
          url.searchParams.set(k, String(v));
        }
      }
    }
    const resp = await this._fetch(url.toString(), {
      method: "GET",
      headers: this.headers(),
    });
    this.checkVersion(resp);
    await this.raiseForStatus(resp);
    return resp.json();
  }

  async _post(path: string, body?: unknown): Promise<unknown> {
    const url = this.baseUrl + path;
    const resp = await this._fetch(url, {
      method: "POST",
      headers: { ...this.headers(), "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    this.checkVersion(resp);
    await this.raiseForStatus(resp);
    if (resp.status === 204) return undefined;
    return resp.json();
  }

  async _delete(path: string): Promise<void> {
    const url = this.baseUrl + path;
    const resp = await this._fetch(url, {
      method: "DELETE",
      headers: this.headers(),
    });
    this.checkVersion(resp);
    await this.raiseForStatus(resp);
  }
}


// --- Resource namespaces -------------------------------------------


class RunsResource {
  constructor(private readonly client: Client) {}

  async create(req: CreateRunRequest): Promise<CreateRunResponse> {
    const data = (await this.client._post("/v1/runs", {
      task: req.task,
      description: req.description,
      model: req.model,
      effort: req.effort,
      paths: req.paths,
    })) as Record<string, unknown>;
    return camelize<CreateRunResponse>(data);
  }

  async get(runId: string): Promise<unknown> {
    return this.client._get(`/v1/runs/${runId}`);
  }
}


class DashboardResource {
  constructor(private readonly client: Client) {}

  async get(): Promise<DashboardResponse> {
    const data = (await this.client._get("/v1/dashboard")) as {
      runs?: Array<Record<string, unknown>>;
    };
    return {
      runs: (data.runs ?? []).map((r) => camelize<DashboardResponse["runs"][number]>(r)),
    };
  }

  async getTeam(opts?: {
    limit?: number;
    team?: string;
  }): Promise<TeamDashboardResponse> {
    const params: Record<string, unknown> = {};
    if (opts?.limit !== undefined) params.limit = opts.limit;
    if (opts?.team !== undefined) params.team = opts.team;
    const raw = (await this.client._get("/v1/dashboard/team", params)) as Record<
      string, unknown
    >;
    const top = camelize<Record<string, unknown>>(raw);
    return {
      orgId: top.orgId as number,
      orgSlug: top.orgSlug as string,
      totalRuns: top.totalRuns as number,
      totalCostUsd: top.totalCostUsd as number,
      byAuthor: (((raw.by_author as Array<Record<string, unknown>>) ?? []).map(
        (a) => camelize<unknown>(a),
      ) as unknown) as TeamDashboardResponse["byAuthor"],
      byTask: (((raw.by_task as Array<Record<string, unknown>>) ?? []).map(
        (t) => camelize<unknown>(t),
      ) as unknown) as TeamDashboardResponse["byTask"],
      recent: (((raw.recent as Array<Record<string, unknown>>) ?? []).map(
        (r) => camelize<unknown>(r),
      ) as unknown) as TeamDashboardResponse["recent"],
    };
  }
}


class KeysResource {
  constructor(private readonly client: Client) {}

  async list(): Promise<KeyMetadata[]> {
    const data = (await this.client._get("/v1/keys")) as {
      keys: Array<Record<string, unknown>>;
    };
    return data.keys.map((k) => camelize<KeyMetadata>(k));
  }

  async create(name: string): Promise<unknown> {
    return this.client._post("/v1/keys", { name });
  }

  async revoke(keyId: string): Promise<void> {
    await this.client._delete(`/v1/keys/${keyId}`);
  }
}


class BudgetResource {
  constructor(private readonly client: Client) {}

  async get(): Promise<unknown> {
    return this.client._get("/v1/budget");
  }
}
