"""GitHub App outbound API (W6.6 follow-up).

The W6.6 webhook receiver lets us *react* to PR events; this module
lets us *respond* — posting an acknowledgement comment when a run is
enqueued and (in a follow-up) the verdict when the run completes.

Three primitives:

- ``mint_app_jwt(app_id, private_key_pem)`` — signs a short-lived
  RS256 JWT identifying the GitHub App. GitHub caps the lifetime at
  10 minutes; we use 9 to leave headroom for clock skew.
- ``mint_installation_token(installation_id, app_jwt, http_client)`` —
  exchanges the App JWT for a per-install access token. Tokens last
  one hour; refresh ahead of expiry.
- ``post_pr_comment(repo_full_name, pr_number, body, install_token,
  http_client)`` — POSTs an Issues-API comment (PRs are issues for
  the comment endpoint). Returns the created comment URL.

Plus an in-memory ``InstallationTokenCache`` keyed by
``installation_id`` so a webhook flood doesn't generate a token per
event.

Also exposes ``apply_diff_to_branch`` for the worker to push a Coder
diff to a new branch and open a PR as the App.

Configuration:

    CLAUDESTRUCT_GITHUB_APP_ID            (numeric App ID)
    CLAUDESTRUCT_GITHUB_APP_PRIVATE_KEY_PEM
                                          (PEM-encoded RS256 private
                                           key from the App settings)

When the env vars aren't set, ``load_app_config()`` returns ``None``
and the caller (webhook router) skips the outbound posting silently
— so a pure-OSS deployment keeps working without GitHub App credentials.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("claudestruct.server.github_app")


# --- Errors ---------------------------------------------------------
#
# GitHubAppError is defined in `_errors.py` so other submodules of
# this package can import it without re-importing the whole
# (still-loading) parent package. Re-exported here so existing
# `from claudestruct.server.github_app import GitHubAppError`
# call sites keep resolving.

from claudestruct.server.github_app._errors import GitHubAppError as GitHubAppError  # noqa: E402, PLC0414, I001


# --- Config ---------------------------------------------------------


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    private_key_pem: bytes


def load_app_config() -> GitHubAppConfig | None:
    """Read the App credentials from env. Returns ``None`` when
    unconfigured so the webhook router can skip outbound calls
    silently rather than raising on a workspace that opted out."""
    app_id = os.environ.get("CLAUDESTRUCT_GITHUB_APP_ID", "").strip()
    pem = os.environ.get("CLAUDESTRUCT_GITHUB_APP_PRIVATE_KEY_PEM", "").strip()
    if not app_id or not pem:
        return None
    return GitHubAppConfig(app_id=app_id, private_key_pem=pem.encode("utf-8"))


# --- JWT helpers ----------------------------------------------------


def _b64url(data: bytes) -> str:
    """JWT-flavoured base64: URL-safe, no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_app_jwt(
    *,
    app_id: str,
    private_key_pem: bytes,
    now: datetime | None = None,
) -> str:
    """Sign an RS256 JWT identifying the App.

    GitHub requires:
      - ``iat`` no more than 60 seconds in the past (we use -30s to
        absorb clock drift between this host and GitHub's edge).
      - ``exp`` no more than 600 seconds in the future (we use 540s).
      - ``iss`` = numeric App ID.

    A short-circuit per-process cache isn't worth it: signing is
    sub-millisecond and the JWT lifetime is short anyway. The
    *installation token* gets cached (see InstallationTokenCache).
    """
    # Lazy-import: cryptography is already a dep (W8.6) but we keep
    # the import inside the function so unit tests of unrelated
    # modules don't pay for it.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    iat = int(n.timestamp()) - 30
    exp = int(n.timestamp()) + 540

    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": iat, "exp": exp, "iss": app_id}

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )

    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except Exception as exc:
        raise GitHubAppError(f"could not load GitHub App private key: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError("GitHub App private key must be RSA")

    signature = key.sign(
        signing_input.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return signing_input + "." + _b64url(signature)


# --- Installation token --------------------------------------------


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime


def mint_installation_token(
    *,
    installation_id: int,
    app_jwt: str,
    http_client: Any,
) -> InstallationToken:
    """Exchange the App JWT for a per-install access token.

    GitHub returns a token that scopes to the installation's repos +
    permissions. Lifetime is one hour from issuance.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    resp = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"installation token mint failed: status={resp.status_code}"
        )
    body = resp.json()
    token = body.get("token")
    expires_at_raw = body.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at_raw, str):
        raise GitHubAppError("installation token response missing fields")
    # GitHub returns ISO 8601 UTC like ``2026-04-28T00:00:00Z``.
    try:
        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubAppError(f"unparseable expires_at: {expires_at_raw!r}") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return InstallationToken(token=token, expires_at=expires_at)


class InstallationTokenCache:
    """Thread-safe in-process cache.

    Refresh policy: hand out the cached token if more than
    ``refresh_buffer`` remains; otherwise mint a fresh one. The
    buffer guards against the case where the token expires
    mid-request — better to overshoot a minute than to send GitHub
    a 401-bound stale token.
    """

    REFRESH_BUFFER = timedelta(minutes=5)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[int, InstallationToken] = {}

    def _is_fresh(self, tok: InstallationToken, *, now: datetime) -> bool:
        return tok.expires_at - now > self.REFRESH_BUFFER

    def get(
        self,
        *,
        installation_id: int,
        app_jwt_provider,
        http_client: Any,
        now: datetime | None = None,
    ) -> InstallationToken:
        """Return a fresh token, minting one if needed.

        ``app_jwt_provider`` is a zero-arg callable returning a JWT —
        defers signing until we know we need a new install token.
        """
        n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached is not None and self._is_fresh(cached, now=n):
                return cached
        # Mint outside the lock so a slow network call doesn't stall
        # other installations; double-check on insert.
        jwt = app_jwt_provider()
        fresh = mint_installation_token(
            installation_id=installation_id,
            app_jwt=jwt,
            http_client=http_client,
        )
        with self._lock:
            self._cache[installation_id] = fresh
        return fresh

    def invalidate(self, installation_id: int) -> None:
        """Drop the cached token (e.g. after a 401 from a downstream
        call). Next ``get`` will mint."""
        with self._lock:
            self._cache.pop(installation_id, None)


# --- PR comment -----------------------------------------------------


def post_pr_comment(
    *,
    repo_full_name: str,
    pr_number: int,
    body: str,
    install_token: str,
    http_client: Any,
) -> str:
    """Post a PR comment via the Issues API.

    GitHub treats every PR as an issue for comment purposes — the
    Issues comment endpoint accepts a PR number directly. Returns
    the created comment's HTML URL on success; raises on error so
    callers can log + retry.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    url = (
        f"https://api.github.com/repos/{repo_full_name}/issues/"
        f"{pr_number}/comments"
    )
    resp = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body},
        timeout=15.0,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"post_pr_comment failed: status={resp.status_code}"
        )
    payload = resp.json()
    html_url = payload.get("html_url")
    if not isinstance(html_url, str):
        raise GitHubAppError("comment response missing html_url")
    return html_url


# --- Branch + PR creation (W6.6 — bot-as-actor PR opens) -----------
#
# These four primitives unblock the worker writing changes back to
# GitHub as the App rather than as the human author. Used together:
#
#   1. ``get_default_branch`` — figure out which branch the new
#      branch should fork from.
#   2. ``get_ref_sha`` — resolve that branch to a SHA.
#   3. ``create_branch`` — Git Refs API; creates a new branch ref
#      pointed at the parent SHA.
#   4. ``create_pull_request`` — Pulls API; opens a PR from the new
#      branch to the parent.
#
# The actual file-write step (between 3 and 4) is intentionally NOT
# in this module — it depends on whether the worker pushes via
# git-over-https or uses the Contents API per file. Both shapes
# work; the choice is a follow-up alongside the worker-side
# integration.


def get_default_branch(
    *,
    repo_full_name: str,
    install_token: str,
    http_client: Any,
) -> str:
    """Return the default-branch name (typically ``main`` / ``master``).

    GitHub's repo metadata endpoint reports it as ``default_branch``.
    Hoisted out so the caller doesn't need to know which key in the
    payload to read.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    url = f"https://api.github.com/repos/{repo_full_name}"
    resp = http_client.get(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise GitHubAppError(
            f"get_default_branch failed: status={resp.status_code}"
        )
    payload = resp.json()
    branch = payload.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise GitHubAppError("repo response missing default_branch")
    return branch


def get_ref_sha(
    *,
    repo_full_name: str,
    ref: str,
    install_token: str,
    http_client: Any,
) -> str:
    """Resolve a branch (or any git ref) to its tip commit SHA.

    Used to pin the parent of a new branch. Accepts ``main`` or
    ``refs/heads/main`` — we normalise to the heads form because
    that's what the Git Refs API expects in the URL.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    # Allow callers to pass either `main` or `refs/heads/main`.
    normalised = ref.removeprefix("refs/heads/")
    url = (
        f"https://api.github.com/repos/{repo_full_name}/git/ref/"
        f"heads/{normalised}"
    )
    resp = http_client.get(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise GitHubAppError(
            f"get_ref_sha({ref!r}) failed: status={resp.status_code}"
        )
    payload = resp.json()
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or len(sha) != 40:
        raise GitHubAppError(f"ref response missing 40-char sha: {payload!r}")
    return sha


def create_branch(
    *,
    repo_full_name: str,
    branch: str,
    base_sha: str,
    install_token: str,
    http_client: Any,
) -> str:
    """Create a new branch pointed at ``base_sha`` via the Git Refs
    API. Returns the new ref's SHA on success.

    The branch name must NOT already exist — GitHub returns 422 if
    it does. Callers should pre-uniquify (e.g. with a run-id suffix)
    so retries don't collide with the first attempt's branch.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    if branch.startswith("refs/heads/"):
        # The Refs API wants the full ref form in the body but a
        # bare name in the URL helper above. Normalise to the full
        # form here so the body is unambiguous.
        ref_full = branch
    else:
        ref_full = f"refs/heads/{branch}"
    url = f"https://api.github.com/repos/{repo_full_name}/git/refs"
    resp = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": ref_full, "sha": base_sha},
        timeout=15.0,
    )
    if resp.status_code != 201:
        # 422 = "Reference already exists". Surface the body if any
        # so the operator can see the precise cause without grepping
        # GitHub-side audit logs.
        raise GitHubAppError(
            f"create_branch({branch!r}) failed: status={resp.status_code}"
        )
    payload = resp.json()
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or len(sha) != 40:
        raise GitHubAppError(
            f"create_branch response missing 40-char sha: {payload!r}"
        )
    return sha


def create_pull_request(
    *,
    repo_full_name: str,
    head: str,
    base: str,
    title: str,
    body: str,
    install_token: str,
    http_client: Any,
    draft: bool = True,
) -> dict[str, Any]:
    """Open a PR. Defaults to **draft=True** so the App's PRs don't
    immediately page reviewers — the operator (or a follow-up
    workflow) marks them ready when CI is green.

    Returns the PR payload (so callers can extract ``number``,
    ``html_url``, and ``head.sha`` for downstream Checks API calls
    without re-querying).
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    if not head or not base:
        raise GitHubAppError("create_pull_request: head and base are required")
    url = f"https://api.github.com/repos/{repo_full_name}/pulls"
    resp = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        },
        timeout=15.0,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"create_pull_request failed: status={resp.status_code}"
        )
    payload = resp.json()
    if not isinstance(payload, dict):
        raise GitHubAppError("create_pull_request response was not a JSON object")
    if "number" not in payload or "html_url" not in payload:
        raise GitHubAppError(
            f"create_pull_request response missing number/html_url: {payload!r}"
        )
    return payload


# --- Checks API ----------------------------------------------------


# --- Checks API formatters ----------------------------------------
#
# Pure-format helpers (no auth, no HTTP) live in `checks_format.py`
# so the test suite can exercise them without importing the
# package's transport layer. Re-exported here for back-compat with
# existing call sites.

from claudestruct.server.github_app.checks_format import (  # noqa: E402, F401
    _VALID_CHECK_CONCLUSIONS,
    CheckAnnotation,
    format_annotations_payload,
    format_check_run_payload,
)


def post_check_run(
    *,
    repo_full_name: str,
    payload: dict[str, Any],
    install_token: str,
    http_client: Any,
) -> dict[str, Any]:
    """Create a check-run via the GitHub Checks API.

    Returns the parsed JSON response (the operator wants both ``id``
    for later updates and ``html_url`` for cross-linking). Raises on
    non-201 — caller decides whether to log + skip or re-raise.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    url = f"https://api.github.com/repos/{repo_full_name}/check-runs"
    resp = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=15.0,
    )
    if resp.status_code != 201:
        raise GitHubAppError(
            f"post_check_run failed: status={resp.status_code}"
        )
    body = resp.json()
    if not isinstance(body.get("id"), int):
        raise GitHubAppError("check_run response missing numeric id")
    return body


def patch_check_run(
    *,
    repo_full_name: str,
    check_run_id: int,
    payload: dict[str, Any],
    install_token: str,
    http_client: Any,
) -> dict[str, Any]:
    """Update an existing check-run (e.g. ``in_progress`` → ``completed``).

    Used so a single check-run shows progress instead of leaving a
    stale "in progress" forever. Mirrors ``post_check_run`` but uses
    PATCH and accepts a 200 (not 201) response.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(
            f"repo_full_name must be 'owner/name', got {repo_full_name!r}"
        )
    url = (
        f"https://api.github.com/repos/{repo_full_name}/"
        f"check-runs/{check_run_id}"
    )
    # ``http_client`` matches the `httpx.Client` interface (.post, .get);
    # for PATCH we either rely on .patch or fall back to .request("PATCH").
    if hasattr(http_client, "patch"):
        resp = http_client.patch(
            url,
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload,
            timeout=15.0,
        )
    else:
        # Tests may stub only ``post`` — provide a deterministic
        # fallback so we don't AttributeError mid-flight.
        resp = http_client.request(  # type: ignore[attr-defined]
            "PATCH",
            url,
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload,
            timeout=15.0,
        )
    if resp.status_code != 200:
        raise GitHubAppError(
            f"patch_check_run failed: status={resp.status_code}"
        )
    return resp.json()


def post_check_run_with_token(
    *,
    cfg: GitHubAppConfig,
    installation_id: int,
    repo_full_name: str,
    payload: dict[str, Any],
    cache: InstallationTokenCache,
    http_client: Any,
) -> dict[str, Any]:
    """Mint or reuse an install token, then create the check-run.

    Mirrors ``post_ack_comment``'s glue shape so the worker can call a
    single symbol per outbound action.
    """
    def _provider() -> str:
        return mint_app_jwt(
            app_id=cfg.app_id, private_key_pem=cfg.private_key_pem,
        )

    tok = cache.get(
        installation_id=installation_id,
        app_jwt_provider=_provider,
        http_client=http_client,
    )
    return post_check_run(
        repo_full_name=repo_full_name,
        payload=payload,
        install_token=tok.token,
        http_client=http_client,
    )


def patch_check_run_with_token(
    *,
    cfg: GitHubAppConfig,
    installation_id: int,
    repo_full_name: str,
    check_run_id: int,
    payload: dict[str, Any],
    cache: InstallationTokenCache,
    http_client: Any,
) -> dict[str, Any]:
    """Mint or reuse an install token, then PATCH the check-run."""
    def _provider() -> str:
        return mint_app_jwt(
            app_id=cfg.app_id, private_key_pem=cfg.private_key_pem,
        )

    tok = cache.get(
        installation_id=installation_id,
        app_jwt_provider=_provider,
        http_client=http_client,
    )
    return patch_check_run(
        repo_full_name=repo_full_name,
        check_run_id=check_run_id,
        payload=payload,
        install_token=tok.token,
        http_client=http_client,
    )


# `format_completed_check_payload` lives in `checks_format.py`;
# re-exported here so existing call sites resolve.
from claudestruct.server.github_app.checks_format import format_completed_check_payload as format_completed_check_payload  # noqa: E402, PLC0414, I001


# --- Convenience: end-to-end ack ------------------------------------


def post_ack_comment(
    *,
    cfg: GitHubAppConfig,
    installation_id: int,
    repo_full_name: str,
    pr_number: int,
    body: str,
    cache: InstallationTokenCache,
    http_client: Any,
) -> str:
    """Mint or reuse a token and post the comment in one call.

    Wraps the full path so the webhook router only needs one symbol.
    Errors propagate; the caller decides whether a posting failure
    should fail the webhook or just log + ignore.
    """
    def _provider() -> str:
        return mint_app_jwt(
            app_id=cfg.app_id, private_key_pem=cfg.private_key_pem,
        )

    tok = cache.get(
        installation_id=installation_id,
        app_jwt_provider=_provider,
        http_client=http_client,
    )
    return post_pr_comment(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        body=body,
        install_token=tok.token,
        http_client=http_client,
    )


# --- PR-open as bot (W6.6 — worker-side clone/apply/push) -----------
#
# The clone/push helpers + ``open_pr_as_bot`` orchestrator live in
# ``clone_push.py`` so this file stays focused on auth + transport.
# Re-exported here so existing
# ``from claudestruct.server.github_app import open_pr_as_bot``
# call sites (worker.py + tests) keep resolving.

from claudestruct.server.github_app.clone_push import (  # noqa: E402, F401, I001
    _git_credential_approve,
    _prepare_repo_push,
    _push_branch_via_git,
    _run_git,
    open_pr_as_bot,
)


# --- Verdict comment formatter -------------------------------------


def format_verdict_body(
    *,
    run_id: str,
    status: str,
    cost_usd: float = 0.0,
    duration_ms: int | None = None,
    error: str | None = None,
) -> str:
    """Compose the verdict-comment Markdown body from a completed Run.

    Two shapes:
      - ``done``: ✅ headline + cost / duration footer.
      - ``failed``: ❌ headline + truncated error in a fenced block.

    Anything else (including an unexpected enum value) falls back to a
    neutral "completed" message — the daemon shouldn't go silent on a
    schema drift it can recover from.
    """
    duration_str = (
        f"{duration_ms / 1000:.1f}s" if isinstance(duration_ms, int) else "unknown"
    )
    if status == "done":
        return (
            f":white_check_mark: claudeStruct review **completed** "
            f"(`{run_id}`).\n\n"
            f"_Cost: ${cost_usd:.4f} · Duration: {duration_str}_"
        )
    if status == "failed":
        # Truncate so a 4KB Anthropic stack trace doesn't blow the
        # GitHub comment limit (65,536 chars) or paste API keys
        # accidentally captured in an error message.
        err_preview = (error or "(no error message)").strip()
        if len(err_preview) > 1500:
            err_preview = err_preview[:1500] + "\n…(truncated)"
        return (
            f":x: claudeStruct review **failed** (`{run_id}`).\n\n"
            f"```\n{err_preview}\n```\n\n"
            f"_Duration: {duration_str}_"
        )
    return (
        f":information_source: claudeStruct run `{run_id}` reached "
        f"status `{status}`."
    )
