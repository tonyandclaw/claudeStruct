"""Tests for Google OAuth (W6.4 follow-on).

Identical coverage to the existing GitHub OAuth test block:
- Configured/unconfigured 503 on login
- State-mismatch 400 on callback
- Unregistered email 403 on callback
- Token-exchange failure 400
- Happy-path sets session cookie
- Session cookie authenticates dashboard
- /me returns principal
- /me 401 without session
- Logout revokes session
- Logout idempotent without cookie
- Bearer wins over session
- Revoked session falls through to 401
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from claudestruct.server.app import create_app
from claudestruct.server.auth import generate_key
from claudestruct.server.db import init_db, make_session_factory
from claudestruct.server.models import ApiKey, Membership, Org, Role, User


@pytest.fixture()
def env(tmp_path):
    """Per-test in-memory SQLite + temp run_root."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        org_a = Org(slug="org-a", name="Org A")
        session.add(org_a)
        session.flush()

        admin = User(email="admin@a")
        session.add(admin)
        session.flush()
        session.add(Membership(
            user_id=admin.id, org_id=org_a.id, role=Role.admin.value,
        ))
        full, key_id, hashed = generate_key()
        session.add(ApiKey(
            user_id=admin.id, org_id=org_a.id,
            key_id=key_id, hashed_secret=hashed, name="admin@a-key",
        ))
        session.commit()

    app = create_app(engine=engine, run_root=str(tmp_path), skip_init=True)
    client = TestClient(app)

    return {"app": app, "client": client, "factory": factory}


class _StubResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _StubGoogleClient:
    """Fake httpx.Client driven from a per-test scripted plan."""

    def __init__(self, plan: list[tuple[Any, _StubResponse]]):
        self.plan = plan
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._dispatch("POST", url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._dispatch("GET", url)

    def _dispatch(self, method, url):
        for pred, resp in self.plan:
            if pred(method, url):
                return resp
        return _StubResponse(500, {"error": "no plan match"})

    def close(self):
        pass


def _wire_google_oauth(env, monkeypatch, *, client: _StubGoogleClient):
    """Set the env vars the OAuth helpers consume + inject the stub."""
    monkeypatch.setenv("CLAUDESTRUCT_GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CLAUDESTRUCT_GOOGLE_OAUTH_SECRET", "test-client-secret")
    monkeypatch.setenv("CLAUDESTRUCT_GOOGLE_OAUTH_REDIRECT_BASE", "http://daemon.test")
    env["app"].state.oauth_http_client = lambda: client


# --- Login ----------------------------------------------------------


def test_google_login_redirects_to_google_when_configured(env, monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_GOOGLE_OAUTH_CLIENT_ID", "x")
    monkeypatch.setenv("CLAUDESTRUCT_GOOGLE_OAUTH_SECRET", "y")
    r = env["client"].get("/v1/auth/google/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    assert "claudestruct_oauth_state=" in r.headers.get("set-cookie", "")


def test_google_login_503_when_unconfigured(env, monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CLAUDESTRUCT_GOOGLE_OAUTH_SECRET", raising=False)
    r = env["client"].get("/v1/auth/google/login", follow_redirects=False)
    assert r.status_code == 503


# --- Callback ------------------------------------------------------


def test_google_callback_state_mismatch_400(env, monkeypatch):
    _wire_google_oauth(env, monkeypatch, client=_StubGoogleClient([]))
    r = env["client"].get(
        "/v1/auth/google/callback?code=abc&state=def",
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_google_callback_unregistered_email_403(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "token" in u,
         _StubResponse(200, {"access_token": "google-token"})),
        (lambda m, u: m == "GET" and "userinfo" in u,
         _StubResponse(200, {
             "email": "stranger@example.com", "name": "Stranger",
         })),
    ]
    _wire_google_oauth(env, monkeypatch, client=_StubGoogleClient(plan))

    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/google/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "not registered" in r.json()["detail"]


def test_google_callback_token_exchange_failure(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "token" in u,
         _StubResponse(400, {"error": "bad_verification_code"})),
    ]
    _wire_google_oauth(env, monkeypatch, client=_StubGoogleClient(plan))
    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/google/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "oauth error" in r.json()["detail"]


def test_google_callback_happy_path_sets_session_cookie(env, monkeypatch):
    plan = [
        (lambda m, u: m == "POST" and "token" in u,
         _StubResponse(200, {"access_token": "google-token"})),
        (lambda m, u: m == "GET" and "userinfo" in u,
         _StubResponse(200, {
             "email": "admin@a", "name": "Admin Alice",
         })),
    ]
    _wire_google_oauth(env, monkeypatch, client=_StubGoogleClient(plan))

    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/google/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "claudestruct_session=" in set_cookie
    assert "HttpOnly" in set_cookie


# --- Session cookie auth --------------------------------------------


def _login_via_google_callback(env, monkeypatch, email="admin@a"):
    """Drive the full OAuth flow and return the session cookie value."""
    plan = [
        (lambda m, u: m == "POST" and "token" in u,
         _StubResponse(200, {"access_token": "google-token"})),
        (lambda m, u: m == "GET" and "userinfo" in u,
         _StubResponse(200, {"email": email, "name": "Test User"})),
    ]
    _wire_google_oauth(env, monkeypatch, client=_StubGoogleClient(plan))
    env["client"].cookies.set(
        "claudestruct_oauth_state", "s123", path="/v1/auth/",
    )
    r = env["client"].get(
        "/v1/auth/google/callback?code=abc&state=s123",
        follow_redirects=False,
    )
    assert r.status_code == 302
    return env["client"].cookies.get("claudestruct_session")


def test_google_session_cookie_authenticates(env, monkeypatch):
    cookie = _login_via_google_callback(env, monkeypatch)
    assert cookie
    r = env["client"].get("/v1/dashboard")
    assert r.status_code == 200


def test_google_whoami_returns_principal(env, monkeypatch):
    _login_via_google_callback(env, monkeypatch, email="admin@a")
    r = env["client"].get("/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "admin@a"
    assert body["org_slug"] == "org-a"
    assert body["role"] == "admin"
    assert body["auth_method"] == "session"


def test_google_whoami_401_without_session(env, monkeypatch):
    r = env["client"].get("/v1/auth/me")
    assert r.status_code == 401


# --- Logout ---------------------------------------------------------


def test_google_logout_revokes_session(env, monkeypatch):
    _login_via_google_callback(env, monkeypatch)
    r1 = env["client"].get("/v1/dashboard")
    assert r1.status_code == 200
    r2 = env["client"].post("/v1/auth/logout")
    assert r2.status_code == 200
    r3 = env["client"].get("/v1/dashboard")
    assert r3.status_code == 401


def test_google_logout_idempotent_without_cookie(env):
    env["client"].cookies.clear()
    r = env["client"].post("/v1/auth/logout")
    assert r.status_code == 200


# --- Auth chain priority --------------------------------------------


def test_bearer_wins_over_google_session(env, monkeypatch):
    _login_via_google_callback(env, monkeypatch, email="admin@a")
    # Mint a viewer key and use it — bearer should win.
    from claudestruct.server.auth import generate_key
    from claudestruct.server.models import ApiKey, Membership, Role, User
    with env["factory"]() as session:
        viewer = User(email="viewer@org-a")
        session.add(viewer)
        session.flush()
        org_a = session.query(Membership).first()
        org = session.get(Membership, org_a.id).org
        session.add(Membership(
            user_id=viewer.id, org_id=org.id, role=Role.viewer.value,
        ))
        full, key_id, hashed = generate_key()
        session.add(ApiKey(
            user_id=viewer.id, org_id=org.id,
            key_id=key_id, hashed_secret=hashed, name="viewer@org-a-key",
        ))
        session.commit()
    r = env["client"].get(
        "/v1/dashboard",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert r.status_code == 200


def test_revoked_google_session_falls_through_to_401(env, monkeypatch):
    cookie = _login_via_google_callback(env, monkeypatch)
    env["client"].post("/v1/auth/logout")
    env["client"].cookies.set("claudestruct_session", cookie, path="/")
    r = env["client"].get("/v1/dashboard")
    assert r.status_code == 401
