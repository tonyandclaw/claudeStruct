// Wire types mirror server/schema.py 1:1, but we expose camelCase
// to TypeScript callers since that's the language convention.
// The boundary conversion lives in client.ts.

export interface KeyMetadata {
  keyId: string;
  name: string;
  createdAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
}

export interface RunRow {
  runId: string;
  startedAt: string | null;
  endedAt: string | null;
  task: string;
  model: string;
  effort: string | null;
  reason: string;
  durationMs: number | null;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  costUsd: number;
  cacheWarnings: string[];
}

export interface AuthorRollup {
  userId: number;
  email: string;
  runs: number;
  costUsd: number;
  inputTokens: number;
  outputTokens: number;
}

export interface TaskRollup {
  task: string;
  runs: number;
  costUsd: number;
}

export interface TeamDashboardResponse {
  orgId: number;
  orgSlug: string;
  totalRuns: number;
  totalCostUsd: number;
  byAuthor: AuthorRollup[];
  byTask: TaskRollup[];
  recent: RunRow[];
}

export interface DashboardResponse {
  runs: RunRow[];
}

export interface CreateRunRequest {
  task: string;
  description: string;
  model?: string;
  effort?: string;
  paths?: string[];
}

export interface CreateRunResponse {
  runId: string;
  status: string;
  note?: string;
}
