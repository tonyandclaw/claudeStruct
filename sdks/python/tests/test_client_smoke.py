"""Smoke tests for the SDK skeleton.

The SDK is pre-stable (0.0.1a0) — these tests lock the public
surface (resource namespaces, exception hierarchy, default
behaviour) so a future refactor can't silently break the shape
documented in the README.

Run from the SDK dir:
    cd sdks/python && python -m pytest
"""
from __future__ import annotations

import warnings
from typing import Any

import pytest

# Allow `pytest` to find the SDK source without pip-installing it.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claudestruct_sdk import (  # noqa: E402
    AuthError,
    BudgetExceededError,
    Client,
    NotFoundError,
)
from claudestruct_sdk.client import _raise_for_status  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, body: Any | None = None,
                 headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            return {}
        return self._body


class _FakeHttp:
    """Minimal httpx.Client stand-in. Records every call and replays
    a queue of (status, body) tuples in order."""

    def __init__(self, responses: list[tuple[int, Any]] | None = None):
        self._responses = responses or [(200, {})]
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def _next(self):
        if not self._responses:
            return _FakeResponse(200, {})
        status, body = self._responses.pop(0)
        return _FakeResponse(
            status, body, headers={"X-CS-Api-Version": "v1"},
        )

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, headers or {}, params))
        return self._next()

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers or {}, json))
        return self._next()

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, headers or {}, None))
        return self._next()

    def close(self):
        pass


# --- Constructor invariants ----------------------------------------


def test_client_requires_base_url_and_api_key():
    with pytest.raises(ValueError):
        Client(base_url="", api_key="ck_test")
    with pytest.raises(ValueError):
        Client(base_url="https://e", api_key="")


def test_client_strips_trailing_slash_from_base_url():
    c = Client(base_url="https://e/", api_key="ck_x", http_client=_FakeHttp())
    assert c.base_url == "https://e"


def test_client_exposes_documented_resource_namespaces():
    """README pins .runs / .dashboard / .keys / .budget — locking
    the surface so a refactor can't silently rename one."""
    c = Client(base_url="https://e", api_key="ck_x", http_client=_FakeHttp())
    assert hasattr(c, "runs")
    assert hasattr(c, "dashboard")
    assert hasattr(c, "keys")
    assert hasattr(c, "budget")


# --- Status-code → typed-exception mapping --------------------------


def test_raise_for_status_2xx_no_raise():
    _raise_for_status(_FakeResponse(200, {}))
    _raise_for_status(_FakeResponse(204, None))


def test_raise_for_status_401_is_authError():
    with pytest.raises(AuthError):
        _raise_for_status(_FakeResponse(401, {"detail": "bad bearer"}))


def test_raise_for_status_402_is_budgetexceedederror():
    with pytest.raises(BudgetExceededError):
        _raise_for_status(_FakeResponse(402, {"detail": "cap reached"}))


def test_raise_for_status_404_is_notfounderror():
    with pytest.raises(NotFoundError):
        _raise_for_status(_FakeResponse(404, {"detail": "team not found"}))


def test_raise_for_status_handles_non_json_body():
    """Server may return text/html on a 502 from the LB. The SDK
    must surface ApiError without crashing on the body parse."""
    class _Bad:
        status_code = 502
        headers: dict[str, str] = {}

        def json(self):
            raise ValueError("not json")

    with pytest.raises(Exception):
        _raise_for_status(_Bad())


# --- Auth header + version warning ----------------------------------


def test_auth_header_attached_to_every_request():
    fake = _FakeHttp(responses=[(200, {})])
    c = Client(base_url="https://e", api_key="ck_topsecret", http_client=fake)
    c._get("/v1/dashboard")
    method, url, headers, _ = fake.calls[0]
    assert method == "GET"
    assert url == "https://e/v1/dashboard"
    assert headers["Authorization"] == "Bearer ck_topsecret"


def test_warns_when_server_version_drifts():
    """If `X-CS-Api-Version` from the server doesn't match what the
    client targets, warn (don't crash) — caller might be running a
    pinned SDK against a freshly-deployed server."""

    class _DriftHttp(_FakeHttp):
        def _next(self):
            return _FakeResponse(
                200, {}, headers={"X-CS-Api-Version": "v2"},
            )

    c = Client(
        base_url="https://e", api_key="ck_x", http_client=_DriftHttp(),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        c._get("/v1/dashboard")
    assert any("v2" in str(w.message) for w in caught)


# --- Resource methods ----------------------------------------------


def test_runs_create_posts_to_v1_runs():
    fake = _FakeHttp(responses=[(202, {"run_id": "abc", "status": "queued"})])
    c = Client(base_url="https://e", api_key="ck_x", http_client=fake)
    res = c.runs.create(task="dev", description="add retry")
    assert res.run_id == "abc"
    method, url, _, body = fake.calls[0]
    assert method == "POST"
    assert url == "https://e/v1/runs"
    assert body == {"task": "dev", "description": "add retry"}


def test_dashboard_get_team_includes_team_param():
    fake = _FakeHttp(responses=[(200, {
        "org_id": 1, "org_slug": "acme",
        "total_runs": 0, "total_cost_usd": 0.0,
        "by_author": [], "by_task": [], "recent": [],
    })])
    c = Client(base_url="https://e", api_key="ck_x", http_client=fake)
    c.dashboard.get_team(team="platform", limit=20)
    method, url, _, params = fake.calls[0]
    assert url == "https://e/v1/dashboard/team"
    assert params == {"limit": 20, "team": "platform"}


def test_keys_revoke_uses_delete():
    fake = _FakeHttp(responses=[(204, None)])
    c = Client(base_url="https://e", api_key="ck_x", http_client=fake)
    c.keys.revoke("key_abc")
    method, url, _, _ = fake.calls[0]
    assert method == "DELETE"
    assert url == "https://e/v1/keys/key_abc"
