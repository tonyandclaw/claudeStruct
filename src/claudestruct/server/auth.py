"""Bearer-token auth + RBAC (W6.3).

API key shape: ``ck_<key_id>_<secret>`` where ``key_id`` is the
user-visible 16-char prefix stored in the DB and ``secret`` is the
high-entropy tail. Only ``sha256(secret)`` is persisted; the full key
is shown once at issuance and can never be retrieved.

The :func:`current_principal` FastAPI dependency authenticates the
request and yields a :class:`Principal` (user + org + role). Routes
that need elevation declare it via :func:`require_role`.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from claudestruct.server.models import ApiKey, Membership, Org, Role, User, UserSession

_KEY_PREFIX = "ck_"
_KEY_ID_BYTES = 8       # 16 hex chars
_KEY_SECRET_BYTES = 24  # 48 hex chars

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Authenticated request context."""

    user_id: int
    user_email: str
    org_id: int
    org_slug: str
    role: Role


def generate_key() -> tuple[str, str, str]:
    """Mint a new (full_key, key_id, hashed_secret) triple.

    The full key is returned exactly once — the caller is expected to
    surface it to the user and discard it. Only ``key_id`` and
    ``hashed_secret`` are persisted in the DB.
    """
    key_id = secrets.token_hex(_KEY_ID_BYTES)
    secret = secrets.token_hex(_KEY_SECRET_BYTES)
    full_key = f"{_KEY_PREFIX}{key_id}_{secret}"
    hashed = _hash_secret(secret)
    return full_key, key_id, hashed


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_key(full_key: str) -> tuple[str, str] | None:
    """Split a presented key into (key_id, secret). Returns None if
    the shape doesn't match."""
    if not full_key.startswith(_KEY_PREFIX):
        return None
    body = full_key[len(_KEY_PREFIX):]
    parts = body.split("_", 1)
    if len(parts) != 2:
        return None
    key_id, secret = parts
    if not key_id or not secret:
        return None
    return key_id, secret


def authenticate(session: Session, full_key: str) -> Principal | None:
    """Look up the key, validate it, and return the Principal.

    Returns None for any failure mode (unknown key, wrong secret,
    revoked, no membership). Caller maps None to 401.
    """
    parsed = parse_key(full_key)
    if not parsed:
        return None
    key_id, secret = parsed

    api_key = session.execute(
        select(ApiKey).where(ApiKey.key_id == key_id)
    ).scalar_one_or_none()
    if api_key is None or not api_key.is_active():
        return None
    if not secrets.compare_digest(api_key.hashed_secret, _hash_secret(secret)):
        return None

    membership = session.execute(
        select(Membership).where(
            Membership.user_id == api_key.user_id,
            Membership.org_id == api_key.org_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        return None

    user = session.get(User, api_key.user_id)
    org = session.get(Org, api_key.org_id)
    if user is None or org is None:
        return None

    api_key.last_used_at = datetime.now(timezone.utc)
    session.commit()

    return Principal(
        user_id=user.id,
        user_email=user.email,
        org_id=org.id,
        org_slug=org.slug,
        role=Role(membership.role),
    )


def get_session(request: Request) -> Session:
    """FastAPI dependency that yields a session from the app's factory.

    The session factory is attached to ``app.state.session_factory``
    by :func:`server.app.create_app`; tests can override it.
    """
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


def authenticate_session_cookie(
    session: Session, cookie_value: str,
) -> Principal | None:
    """Look up a UserSession by cookie value, return its Principal.

    None on every failure mode (unknown cookie, expired, revoked,
    deleted user/org/membership). Cookie value is the
    `session_token` minted by the OAuth callback (W6.4)."""
    sess: UserSession | None = session.execute(
        select(UserSession).where(UserSession.session_token == cookie_value)
    ).scalar_one_or_none()
    if sess is None or not sess.is_active():
        return None

    membership = session.execute(
        select(Membership).where(
            Membership.user_id == sess.user_id,
            Membership.org_id == sess.org_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        return None

    user = session.get(User, sess.user_id)
    org = session.get(Org, sess.org_id)
    if user is None or org is None:
        return None

    return Principal(
        user_id=user.id,
        user_email=user.email,
        org_id=org.id,
        org_slug=org.slug,
        role=Role(membership.role),
    )


_SESSION_COOKIE_NAME = "claudestruct_session"


def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: Session = Depends(get_session),
) -> Principal:
    """Auth chain: bearer token first, cookie session as fallback (W6.4).

    Bearer wins so a CLI script with both a key and a stale cookie
    behaves as the CLI key intends. Cookie path lets the SPA mount
    against the same APIs.
    """
    if creds is not None and creds.scheme.lower() == "bearer":
        principal = authenticate(session, creds.credentials)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    cookie = request.cookies.get(_SESSION_COOKIE_NAME)
    if cookie:
        principal = authenticate_session_cookie(session, cookie)
        if principal is not None:
            return principal

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing bearer token or valid session cookie",
        headers={"WWW-Authenticate": "Bearer"},
    )


_ROLE_RANK = {Role.viewer: 0, Role.member: 1, Role.admin: 2}


def require_role(min_role: Role):
    """Dependency factory. ``Depends(require_role(Role.admin))``
    returns the principal if the role is at least ``min_role``,
    otherwise 403."""

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if _ROLE_RANK[principal.role] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{principal.role.value}' lacks required '{min_role.value}'",
            )
        return principal

    return _dep


# --- Tenant-isolation helpers (C.5) --------------------------------
#
# Every multi-tenant query in this codebase has an `org_id` filter
# attached manually. The helpers below give the filter a name and a
# single import site, so:
#
#   1. A future router that forgets to filter is a single missing
#      import (review-catchable) instead of a missing `.where(...)`
#      buried in a chained query.
#   2. Cross-tenant fetches return 404 (not 403) consistently —
#      the no-leakage rule lives in one place.
#
# Both helpers expect the model to expose `.org_id`. They don't try
# to be generic over arbitrary ownership fields; that would invite
# the misuse "I'll just pass `user_id` here instead" which silently
# breaks org-scoped sharing.


def with_org_scope(stmt, model, principal: Principal):
    """Add ``.where(model.org_id == principal.org_id)`` to a SELECT.

    Use as the LAST step before ``session.execute(...)`` so the
    filter is the visibly-final constraint, not buried under a
    cascade of optional `.where(...)` calls. Returns the modified
    statement so this composes with the SQLAlchemy fluent API.
    """
    return stmt.where(model.org_id == principal.org_id)


def require_org_owned(row, principal: Principal, *, label: str = "resource"):
    """Raise ``HTTPException(404)`` if ``row`` is None or doesn't
    belong to the principal's org.

    Use at the point of fetch — typically right after a
    ``session.get(Model, id)`` or a query that filtered by id but
    not (yet) by org. The 404 is intentional even for a
    cross-tenant hit: a 403 would tell the attacker the id exists
    in *some* org, which is a low-key enumeration oracle.

    `label` is the noun used in the error detail (``"key"``,
    ``"team"``, ``"invoice"``); kept generic so the same helper
    works across all routers.
    """
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    row_org_id = getattr(row, "org_id", None)
    if row_org_id is None:
        # The row's model doesn't expose `org_id` — by the rule in
        # the module docstring, this helper is misapplied. Loud
        # failure rather than silent pass: a model without `org_id`
        # is by definition not tenant-scoped, and the caller should
        # use a different gate.
        raise HTTPException(
            status_code=500,
            detail=(
                f"{type(row).__name__} has no org_id; require_org_owned "
                f"must only be used on tenant-scoped models"
            ),
        )
    if row_org_id != principal.org_id:
        raise HTTPException(status_code=404, detail=f"{label} not found")
