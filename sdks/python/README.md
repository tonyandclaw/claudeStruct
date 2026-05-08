# claudestruct-sdk (Python)

`0.0.1-alpha` — pre-stable preview client for the claudeStruct
hosted REST API. The shape may shift before `0.1.0`; please pin
exact versions until then.

## Install

```bash
pip install claudestruct-sdk  # not yet published; install from this dir
```

## Usage

```python
from claudestruct_sdk import Client

client = Client(
    base_url="https://api.claudestruct.dev",
    api_key="ck_...",
)

# Submit a run
run = client.runs.create(task="dev", description="add retry to client.py")
print(run.run_id)

# Pull dashboard
view = client.dashboard.get_team()
print(f"{view.total_runs} runs, ${view.total_cost_usd}")

# List API keys
for k in client.keys.list():
    print(k.key_id, k.name)
```

## Design notes

- **Single transport.** Everything goes through `httpx.Client`
  with `timeout=30s` and no retry — retries belong to the
  application, not the SDK. If you want backoff, wrap the
  call site.
- **No magic.** The shape of every response model mirrors the
  OpenAPI spec at `/openapi.json` 1:1. If you want a richer
  type, write a wrapper.
- **Auth is a single header.** Bearer-token only. Cookie auth
  (browser flows) is out of scope for this SDK; use the
  hosted SPA for that surface.
- **Versioned.** `Client(api_version="v1")` for the current
  major. The SDK reads `X-CS-Api-Version` on every response
  and warns if the server is on a higher major than the
  client targets.

## What's in here

- `Client` — entry point
- `Client.runs.create / get / list`
- `Client.dashboard.get_team`
- `Client.keys.list / create / revoke`
- `Client.budget.get`
- Plain dataclasses for every response shape (RunRow,
  TeamDashboardResponse, KeyMetadata, etc.)

## What's NOT in here yet

- OAuth flows (browser cookie path)
- Stripe webhook helpers (the API publishes them; SDK doesn't
  wrap them — they're typically called from server code)
- Streaming responses
- Async client (would be a v0.2 add)
