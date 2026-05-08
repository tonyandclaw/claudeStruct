# @claudestruct/sdk (TypeScript)

`0.0.1-alpha` — pre-stable preview client for the claudeStruct
hosted REST API. Shape may shift before `0.1.0`; please pin
exact versions until then.

## Install

```bash
pnpm add @claudestruct/sdk@0.0.1-alpha   # not yet published
```

## Usage

```ts
import { Client } from "@claudestruct/sdk";

const client = new Client({
  baseUrl: "https://api.claudestruct.dev",
  apiKey: "ck_...",
});

// Submit a run
const run = await client.runs.create({
  task: "dev",
  description: "add retry to client.ts",
});
console.log(run.runId);

// Pull team dashboard
const view = await client.dashboard.getTeam();
console.log(`${view.totalRuns} runs, $${view.totalCostUsd}`);

// List keys
for (const k of await client.keys.list()) {
  console.log(k.keyId, k.name);
}
```

## Design notes

- **Native `fetch`.** No `axios` / `got` / `node-fetch` dep. Node
  18+ ships `fetch` natively; browsers always had it. The bundle
  stays small.
- **No magic.** Response shape mirrors the OpenAPI spec at
  `/openapi.json` 1:1. Field names are camelCase on the
  TypeScript side (the wire format is snake_case; the SDK
  converts at the boundary).
- **Version-aware.** The client reads `X-CS-Api-Version` and
  emits a `console.warn` if the server is on a higher major
  than the SDK targets.

## What's in here

- `Client` — entry point
- `client.runs.create / get`
- `client.dashboard.get / getTeam`
- `client.keys.list / create / revoke`
- `client.budget.get`
- Plain TypeScript types for every response shape

## What's NOT in here yet

- OAuth flows (browser cookie path)
- Stripe webhook helpers (server-side concern)
- Streaming responses
- Retries (intentional — wrap at the call site if needed)
