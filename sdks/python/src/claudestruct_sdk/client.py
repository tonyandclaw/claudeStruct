"""SDK entry point.

Single transport (httpx.Client). Three resource namespaces hung
off the client:

    client.runs          # POST + GET runs
    client.dashboard     # GET dashboard / team dashboard
    client.keys          # list / create / revoke API keys
    client.budget        # GET budget summary
"""
from __future__ import annotations

import warnings
from typing import Any

import httpx

from claudestruct_sdk.errors import (
    ApiError,
    AuthError,
    BudgetExceededError,
    ForbiddenError,
    NotFoundError,
    ServerError,
)
from claudestruct_sdk.models import (
    CreateRunResponse,
    DashboardResponse,
    KeyMetadata,
    TeamDashboardResponse,
    dashboard_from_dict,
    key_metadata_from_dict,
    team_dashboard_from_dict,
)

_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_API_VERSION = "v1"


def _raise_for_status(resp: httpx.Response) -> None:
    if 200 <= resp.status_code < 300:
        return
    detail: str | None = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = body.get("detail") if isinstance(body.get("detail"), str) else None
    except Exception:  # noqa: BLE001
        pass
    code = resp.status_code
    if code == 401:
        raise AuthError(code, detail)
    if code == 402:
        raise BudgetExceededError(code, detail)
    if code == 403:
        raise ForbiddenError(code, detail)
    if code == 404:
        raise NotFoundError(code, detail)
    if 500 <= code < 600:
        raise ServerError(code, detail)
    raise ApiError(code, detail)


class Client:
    """The single SDK entry point.

    Use ``Client(...)`` as a context manager for short scripts; for
    long-running services, share one client across requests so the
    underlying HTTP/2 connection pool is reused.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_version: str = _DEFAULT_API_VERSION,
        timeout: float = _DEFAULT_TIMEOUT_S,
        http_client: Any | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version
        # Caller can inject their own httpx.Client (for tests or to
        # share a pool); otherwise we construct one with sensible
        # defaults.
        self._http: Any = http_client or httpx.Client(timeout=timeout)
        self._owns_http = http_client is None
        self.runs = _RunsResource(self)
        self.dashboard = _DashboardResource(self)
        self.keys = _KeysResource(self)
        self.budget = _BudgetResource(self)

    def close(self) -> None:
        if self._owns_http:
            close = getattr(self._http, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # --- Internal HTTP helpers ----------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _check_version(self, resp: httpx.Response) -> None:
        """Warn (not raise) if the server's major version is ahead
        of what the client targets — the contract may have shifted
        even within a deployment."""
        server = resp.headers.get("X-CS-Api-Version")
        if server and server != self.api_version:
            warnings.warn(
                f"server X-CS-Api-Version={server!r} but SDK targets "
                f"{self.api_version!r}; the response shape may have changed. "
                f"Pin the SDK or upgrade.",
                stacklevel=3,
            )

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._http.get(url, headers=self._headers(), params=params or None)
        self._check_version(resp)
        _raise_for_status(resp)
        return resp.json()

    def _post(self, path: str, body: Any | None = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self._http.post(url, headers=self._headers(), json=body)
        self._check_version(resp)
        _raise_for_status(resp)
        if resp.status_code == 204:
            return None
        return resp.json()

    def _delete(self, path: str) -> None:
        url = f"{self.base_url}{path}"
        resp = self._http.delete(url, headers=self._headers())
        self._check_version(resp)
        _raise_for_status(resp)


# --- Resource namespaces --------------------------------------------


class _RunsResource:
    def __init__(self, client: Client) -> None:
        self._client = client

    def create(
        self,
        *,
        task: str,
        description: str,
        model: str | None = None,
        effort: str | None = None,
        paths: list[str] | None = None,
    ) -> CreateRunResponse:
        body: dict[str, Any] = {"task": task, "description": description}
        if model is not None:
            body["model"] = model
        if effort is not None:
            body["effort"] = effort
        if paths is not None:
            body["paths"] = paths
        data = self._client._post("/v1/runs", body)
        return CreateRunResponse(**data)

    def get(self, run_id: str) -> dict[str, Any]:
        return self._client._get(f"/v1/runs/{run_id}")


class _DashboardResource:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self) -> DashboardResponse:
        return dashboard_from_dict(self._client._get("/v1/dashboard"))

    def get_team(
        self,
        *,
        limit: int | None = None,
        team: str | None = None,
    ) -> TeamDashboardResponse:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if team is not None:
            params["team"] = team
        return team_dashboard_from_dict(
            self._client._get("/v1/dashboard/team", **params),
        )


class _KeysResource:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[KeyMetadata]:
        data = self._client._get("/v1/keys")
        return [key_metadata_from_dict(k) for k in data["keys"]]

    def create(self, name: str) -> dict[str, Any]:
        return self._client._post("/v1/keys", {"name": name})

    def revoke(self, key_id: str) -> None:
        self._client._delete(f"/v1/keys/{key_id}")


class _BudgetResource:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self) -> dict[str, Any]:
        return self._client._get("/v1/budget")
