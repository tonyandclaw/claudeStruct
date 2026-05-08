"""OAuth login + session management (W6.4).

Two-step flow:

  1. ``GET /v1/auth/github/login`` — generate a CSRF state token,
     stash it in a short-lived cookie, redirect to GitHub.
  2. ``GET /v1/auth/github/callback?code=...&state=...`` — verify state
     matches the stashed cookie, exchange code for token, fetch user,
     resolve to a local User row (by email match), mint a session,
     set the ``claudestruct_session`` HTTPOnly cookie, redirect to the
     post-login UI.

Plus:

  - ``GET /v1/auth/me`` — return the principal for an authenticated
    cookie session. Lets the SPA tell whether the user is logged in
    without re-running the OAuth dance.
  - ``POST /v1/auth/logout`` — revoke the current session, clear cookie.

Auth fallback chain in `auth.current_principal`: bearer token wins;
when absent, we look for ``claudestruct_session`` cookie. So an SPA
can mount the same APIs as the CLI.

Auto-provisioning policy:
    A GitHub OAuth login resolves to a local User by **email match**.
    Users who don't exist yet are NOT auto-created — auth fails with
    403 and a "your email isn't a member of any org yet" message. The
    admin must `cs serve add-user <email> <org>` first. This avoids
    one of the worst SaaS-onboarding footguns: any GitHub account in
    the world creating a fresh tenant on your DB just by clicking
    "Sign in with GitHub". W8.7 covers self-serve signup behind a
    domain allowlist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from claudestruct.server import audit as audit_mod
from claudestruct.server import oauth as oauth_mod
from claudestruct.server.models import Membership, Org, User, UserSession

router = APIRouter(prefix="/v1/auth", tags=["auth"])

SESSION_COOKIE_NAME = "claudestruct_session"
STATE_COOKIE_NAME = "claudestruct_oauth_state"
STATE_COOKIE_MAX_AGE = 600  # 10 min — plenty for the GitHub round-trip


def _http_client_factory_default():
    """Lazy-import httpx so the lean install doesn't pay for it."""
    import httpx

    return httpx.Client()


def _http_client(request: Request):
    """Override hook: tests inject a fake client via
    ``app.state.oauth_http_client``."""
    factory = getattr(request.app.state, "oauth_http_client", None)
    if factory is None:
        return _http_client_factory_default()
    return factory()


@router.get("/github/login")
def github_login(request: Request) -> RedirectResponse:
    cfg = oauth_mod.load_github_config()
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail="GitHub OAuth not configured; set CLAUDESTRUCT_GITHUB_OAUTH_CLIENT_ID and _SECRET",
        )
    state = oauth_mod.new_state_token()
    url = oauth_mod.build_authorize_url(cfg, state=state)
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        STATE_COOKIE_NAME,
        state,
        max_age=STATE_COOKIE_MAX_AGE,
        httponly=True,
        # `lax` lets the cookie ride along the same-site GitHub redirect
        # back; `strict` would drop it on the cross-site bounce.
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/v1/auth/",
    )
    return response


@router.get("/github/callback")
def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
) -> Response:
    cfg = oauth_mod.load_github_config()
    if cfg is None:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    cookie_state = request.cookies.get(STATE_COOKIE_NAME)
    if not cookie_state or cookie_state != state:
        # CSRF guard. Empty state cookie means the user landed here
        # directly (or after the state cookie expired); mismatched
        # cookie means an attacker tried to coerce a callback.
        raise HTTPException(status_code=400, detail="state mismatch")

    client = _http_client(request)
    try:
        token = oauth_mod.exchange_code_for_token(cfg, code, http_client=client)
        identity = oauth_mod.fetch_github_user(token, http_client=client)
    except oauth_mod.OAuthError as exc:
        raise HTTPException(status_code=400, detail=f"oauth error: {exc}") from exc
    finally:
        # httpx.Client must be closed; tests' stub clients are no-ops.
        close = getattr(client, "close", None)
        if callable(close):
            close()

    factory = request.app.state.session_factory
    with factory() as session:
        user = session.execute(
            select(User).where(User.email == identity["email"])
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"email {identity['email']!r} is not registered. "
                    "Ask an admin to run `cs serve add-user`."
                ),
            )
        membership = session.execute(
            select(Membership).where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                status_code=403,
                detail=f"user {identity['email']!r} has no org membership",
            )

        session_token = oauth_mod.new_session_token()
        sess_row = UserSession(
            user_id=user.id,
            org_id=membership.org_id,
            session_token=session_token,
            provider="github",
            expires_at=oauth_mod.session_expiry(),
        )
        session.add(sess_row)
        session.flush()
        # Audit the login (A.5 customer-success instrumentation).
        # The session token itself is sensitive — record only the
        # provider + the row id so a support-ticket walk can correlate
        # without exposing the cookie value.
        audit_mod.record(
            session,
            org_id=membership.org_id,
            actor_user_id=user.id,
            action="auth.login.success",
            resource_type="user_session",
            resource_id=str(sess_row.id),
            payload={"provider": "github"},
        )
        session.commit()

    # Redirect back to a generic "/" — the SPA mounted there reads
    # /v1/auth/me to populate state. We don't take a redirect target
    # in the request to avoid open-redirect vulns.
    redirect_target = (
        request.app.state.oauth_post_login_url
        if hasattr(request.app.state, "oauth_post_login_url")
        else "/"
    )
    response = RedirectResponse(redirect_target, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=int(oauth_mod.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    # Clear the state cookie now that it's served its purpose.
    response.delete_cookie(STATE_COOKIE_NAME, path="/v1/auth/")
    return response


@router.get("/me")
def whoami(request: Request) -> dict[str, Any]:
    """Return the principal for the active session cookie.

    Importantly, this does NOT depend on `current_principal` because
    we want the response shape to differ for the SPA — including
    `auth_method` so the client knows whether it can offer a "log
    out" button (cookie sessions can; bearer-token CLI calls can't).
    """
    factory = request.app.state.session_factory
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=401, detail="not authenticated")
    with factory() as session:
        sess: UserSession | None = session.execute(
            select(UserSession).where(UserSession.session_token == cookie)
        ).scalar_one_or_none()
        if sess is None or not sess.is_active():
            raise HTTPException(status_code=401, detail="session expired or revoked")
        user = session.get(User, sess.user_id)
        org = session.get(Org, sess.org_id)
        if user is None or org is None:
            raise HTTPException(status_code=401, detail="session points at deleted user/org")
        membership = session.execute(
            select(Membership).where(
                Membership.user_id == user.id, Membership.org_id == org.id,
            )
        ).scalar_one_or_none()
        role = membership.role if membership else "viewer"
        return {
            "user_id": user.id,
            "email": user.email,
            "name": user.name,
            "org_id": org.id,
            "org_slug": org.slug,
            "role": role,
            "auth_method": "session",
            "session_expires_at": sess.expires_at.isoformat(),
        }


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    """Revoke the active session + clear the cookie.

    Idempotent — calling without a cookie or with an unknown cookie
    still returns 200 since the goal (no active session) is met."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        factory = request.app.state.session_factory
        with factory() as session:
            sess = session.execute(
                select(UserSession).where(UserSession.session_token == cookie)
            ).scalar_one_or_none()
            if sess is not None and sess.revoked_at is None:
                sess.revoked_at = datetime.now(timezone.utc)
                # Audit the logout (A.5). The cookie value itself is
                # not recorded — only the session row id — so the
                # audit trail can be correlated by support without
                # leaking the bearer.
                audit_mod.record(
                    session,
                    org_id=sess.org_id,
                    actor_user_id=sess.user_id,
                    action="auth.logout",
                    resource_type="user_session",
                    resource_id=str(sess.id),
                    payload={"provider": sess.provider},
                )
                session.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}
