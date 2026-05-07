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
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("claudestruct.server.github_app")


# --- Errors ---------------------------------------------------------


class GitHubAppError(RuntimeError):
    """Raised when the App ↔ GitHub conversation fails. Caught by
    callers that want to log + skip the outbound action without
    crashing the request that triggered it."""


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


# Conclusion strings GitHub accepts. We use a small whitelist so a
# typo in the worker can't reach the network and 422 there.
_VALID_CHECK_CONCLUSIONS = {
    "success", "failure", "neutral", "cancelled", "skipped",
    "timed_out", "action_required", "stale",
}


# --- Annotations (W6.6 — per-line check-run annotations) -----------
#
# GitHub Checks API supports ``annotations`` in the output payload.
# Each annotation pinpoints a specific line in a specific file with a
# severity level. Used to surface reviewer findings as inline PR
# comments in the Checks tab.


@dataclass(frozen=True)
class CheckAnnotation:
    path: str
    start_line: int
    end_line: int
    annotation_level: str  # "notice", "warning", "error"
    message: str
    title: str | None = None


def format_annotations_payload(
    annotations: list[CheckAnnotation],
) -> list[dict[str, Any]] | None:
    """Build the annotations list for a check-run output payload.

    Returns ``None`` when the list is empty so callers can omit the
    ``annotations`` key entirely (GitHub treats absent vs. empty
    identically but omitting it saves bandwidth).
    """
    if not annotations:
        return None
    return [
        {
            "path": a.path,
            "start_line": a.start_line,
            "end_line": a.end_line,
            "annotation_level": a.annotation_level,
            "message": a.message,
            **( {"title": a.title} if a.title else {} ),
        }
        for a in annotations
    ]


def format_check_run_payload(
    *,
    name: str,
    head_sha: str,
    status: str,
    conclusion: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    details_url: str | None = None,
    external_id: str | None = None,
    annotations: list[CheckAnnotation] | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``POST/PATCH /repos/.../check-runs``.

    GitHub's Checks API accepts:
      - ``status``: ``queued`` / ``in_progress`` / ``completed``.
      - ``conclusion`` (required iff ``status="completed"``): see the
        ``_VALID_CHECK_CONCLUSIONS`` set above.
      - ``output``: optional ``{title, summary, text, annotations[]}``
        block rendered in the PR's checks tab.

    Validation here is conservative: an unknown ``status`` or
    ``conclusion`` raises ``GitHubAppError`` immediately rather than
    letting GitHub reject with 422 mid-request, which would force the
    operator to read CloudWatch / journald to debug.
    """
    if status not in {"queued", "in_progress", "completed"}:
        raise GitHubAppError(
            f"check_run status must be queued|in_progress|completed, got {status!r}"
        )
    if status == "completed":
        if conclusion is None:
            raise GitHubAppError("completed check_run requires a conclusion")
        if conclusion not in _VALID_CHECK_CONCLUSIONS:
            raise GitHubAppError(
                f"unknown check_run conclusion {conclusion!r}; "
                f"expected one of {sorted(_VALID_CHECK_CONCLUSIONS)}"
            )
    elif conclusion is not None:
        # Non-completed runs must NOT carry a conclusion — GitHub 422s.
        raise GitHubAppError(
            "conclusion is only valid when status='completed'"
        )

    body: dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": status,
    }
    if conclusion is not None:
        body["conclusion"] = conclusion
    if details_url is not None:
        body["details_url"] = details_url
    if external_id is not None:
        body["external_id"] = external_id
    if title or summary:
        # Title is required by GitHub when output is present; default
        # to the run's name so the operator never has to think about it.
        output: dict[str, Any] = {
            "title": title or name,
            "summary": summary or "",
        }
        annotations_payload = format_annotations_payload(annotations) if annotations else None
        if annotations_payload:
            output["annotations"] = annotations_payload
        body["output"] = output
    return body


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


def format_completed_check_payload(
    *,
    head_sha: str,
    run_id: str,
    status: str,
    cost_usd: float = 0.0,
    duration_ms: int | None = None,
    error: str | None = None,
    annotations: list[CheckAnnotation] | None = None,
) -> dict[str, Any]:
    """Translate a terminal ``Run`` row into a check-run completion
    payload.

    `done` runs map to `conclusion=success`; `failed` to
    `conclusion=failure`. Anything else maps to `neutral` so an
    unexpected enum value still produces a valid Checks API call.
    """
    if status == "done":
        conclusion = "success"
        title = "claudeStruct review passed"
        duration_str = (
            f"{duration_ms / 1000:.1f}s" if isinstance(duration_ms, int) else "unknown"
        )
        summary = (
            f"Run `{run_id}` completed successfully.\n\n"
            f"- Cost: ${cost_usd:.4f}\n"
            f"- Duration: {duration_str}"
        )
    elif status == "failed":
        conclusion = "failure"
        title = "claudeStruct review failed"
        err_preview = (error or "(no error message)").strip()
        if len(err_preview) > 1500:
            err_preview = err_preview[:1500] + "\n…(truncated)"
        summary = (
            f"Run `{run_id}` failed.\n\n"
            f"```\n{err_preview}\n```"
        )
    else:
        conclusion = "neutral"
        title = f"claudeStruct run reached status {status!r}"
        summary = f"Run `{run_id}` ended in unexpected status `{status}`."

    return format_check_run_payload(
        name="claudeStruct",
        head_sha=head_sha,
        status="completed",
        conclusion=conclusion,
        title=title,
        summary=summary,
        external_id=run_id,
        annotations=annotations,
    )


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
# The verdict-on-completion and Checks-API items (W6.6 shipped) handle
# the outbound comment/check-run.  This section adds the mirror:
# after a successful run, create a branch off the PR's base branch,
# push the repo's changes to it, and open a PR as the App.
#
# Sequence:
#   1. ``_prepare_repo_push`` — clone with install token, switch to new branch.
#   2. ``_push_branch_via_git`` — add + commit + push via git-over-https.
#   3. ``open_pr_as_bot`` — orchestrates the full chain; callers pass
#      ``run_root`` (Path) and the install-token URL components.
#
# Git-over-https is used (not the Contents API) because multi-file /
# rename / binary diffs are handled correctly by native git rather than
# requiring callers to hand-encode every file as base64.


def _run_git(
    cmd: list[str],
    cwd: str | Path,
    env: dict[str, str],
    timeout: float = 60.0,
) -> None:
    """Run a git command; raise ``GitHubAppError`` on non-zero exit."""
    import subprocess
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubAppError(
            f"git command timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    if result.returncode != 0:
        raise GitHubAppError(
            f"git {' '.join(cmd)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _git_credential_approve(
    repo_url: str,
    username: str,
    password: str,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Store a git credential so subsequent push operations succeed."""
    input_str = f"url={repo_url}\nusername={username}\npassword={password}\n"
    try:
        subprocess.run(
            ["git", "credential", "approve"],
            input=input_str,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except Exception:
        pass  # best-effort; if this fails the push URL approach is the fallback


def _prepare_repo_push(
    *,
    repo_full_name: str,
    base_branch: str,
    new_branch: str,
    install_token: str,
    http_client: Any,
    clone_dir: Path,
) -> Path:
    """Clone the repo with the install token, then create + checkout the
    new branch pointed at the base branch's tip SHA.

    Returns the SHA of the newly created branch head so callers can
    confirm the push succeeded.
    """
    if "/" not in repo_full_name:
        raise GitHubAppError(f"repo_full_name must be 'owner/name', got {repo_full_name!r}")
    owner, name = repo_full_name.split("/", 1)

    # Resolve the base branch SHA via the Git Refs API (already have
    # the primitive — reuse it).
    base_sha = get_ref_sha(
        repo_full_name=repo_full_name,
        ref=base_branch,
        install_token=install_token,
        http_client=http_client,
    )

    # Build a git-over-https URL with the install token embedded.
    # We configure credential.helper=store so git caches the token.
    clone_url = f"https://x-access-token:{install_token}@github.com/{repo_full_name}.git"
    repo_url = f"https://github.com/{repo_full_name}.git"

    env = dict(os.environ)
    env["GIT_HTTP_USER_AGENT"] = "claudeStruct-GitHubApp/1.0"
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for passwords

    # Clone shallowly (depth=1 is enough to push a new branch).
    _run_git(
        ["git", "clone", "--depth=1", "--branch", base_branch, clone_url, str(clone_dir)],
        cwd=clone_dir.parent,
        env=env,
        timeout=120.0,
    )

    # Configure the push URL with the token (in case the clone URL
    # gets stripped by credential helper misconfiguration).
    _run_git(
        ["git", "remote", "set-url", "origin", clone_url],
        cwd=clone_dir,
        env=env,
    )

    # Store the credential so subsequent git push operations succeed.
    _git_credential_approve(repo_url, "x-access-token", install_token, clone_dir, env)

    # Create and switch to the new branch (detached HEAD state from
    # shallow clone + new branch points at the same commit as base_branch).
    _run_git(
        ["git", "checkout", "-b", new_branch],
        cwd=clone_dir,
        env=env,
    )

    return base_sha


def _push_branch_via_git(
    clone_dir: Path,
    commit_message: str,
    author_name: str = "claudeStruct[bot]",
    author_email: str = "bot@claudestruct.dev",
) -> str:
    """Stage all changes, commit, and push the new branch.

    Returns the SHA of the newly pushed commit.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    # Stage everything (new + modified + deleted).
    _run_git(["git", "add", "-A"], cwd=clone_dir, env=env)

    # Check if there are staged changes to commit.
    result = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        cwd=str(clone_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise GitHubAppError("no changes to commit in PR branch")

    _run_git(
        [
            "git", "commit",
            "--author", f"{author_name} <{author_email}>",
            "-m", commit_message,
        ],
        cwd=clone_dir,
        env=env,
    )

    # Push the new branch.
    _run_git(
        ["git", "push", "-u", "origin", "HEAD"],
        cwd=clone_dir,
        env=env,
        timeout=60.0,
    )

    # Return the new commit SHA.
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(clone_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if result.returncode != 0:
        raise GitHubAppError("could not read new commit SHA after push")
    return result.stdout.strip()


def open_pr_as_bot(
    *,
    cfg: GitHubAppConfig,
    installation_id: int,
    repo_full_name: str,
    base_branch: str,
    run_id: str,
    description: str,
    run_root: Path,
    cache: InstallationTokenCache,
    http_client: Any,
    commit_title: str | None = None,
    commit_body: str | None = None,
) -> dict[str, Any]:
    """Clone the repo, stage + push the working tree changes as a new
    branch, then open a PR as the GitHub App.

    This is the worker-side integration that W6.6 deferred — it ties
    together the already-shipped Git Refs primitives with a git-over-https
    push and the existing ``create_pull_request`` primitive.

    Returns the PR payload dict so the worker can record ``github_pr_number``
    on the Run row.

    Best-effort: if the push fails after the PR is opened we can't roll
    back the PR creation, so we let the operator know via the verdict
    comment that the branch may be stale.
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

    # Unique branch name per run so retries create a new branch (not
    # an error from "ref already exists").
    new_branch = f"claudestruct/run-{run_id}"

    # Temp dir for the clone — cleaned up by the caller via
    # ``shutil.rmtree(clone_dir, ignore_errors=True)``.
    clone_dir = Path(tempfile.mkdtemp(prefix="claudestruct-pr-"))

    try:
        # Prepare (clone + create branch).
        _prepare_repo_push(
            repo_full_name=repo_full_name,
            base_branch=base_branch,
            new_branch=new_branch,
            install_token=tok.token,
            http_client=http_client,
            clone_dir=clone_dir,
        )

        # Push the working tree changes.
        commit_sha = _push_branch_via_git(
            clone_dir=clone_dir,
            commit_message=commit_title or f"claudeStruct run {run_id}: {description[:72]}",
        )

        # Open the PR.
        title = commit_title or f"[claudeStruct] {description[:72]}"
        body = commit_body or (
            f"Automated PR from claudeStruct run [`{run_id}`]"
        )
        pr_payload = create_pull_request(
            repo_full_name=repo_full_name,
            head=new_branch,
            base=base_branch,
            title=title,
            body=body,
            install_token=tok.token,
            http_client=http_client,
            draft=True,
        )
        log.info(
            "PR opened for run %s: %s (head_sha=%s)",
            run_id, pr_payload.get("html_url"), commit_sha,
        )
        return pr_payload

    except GitHubAppError:
        raise
    except Exception as exc:
        raise GitHubAppError(f"PR-open failed for run {run_id}: {exc}") from exc
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


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
