"""GitHub App webhook receiver (W6.6).

Accepts ``POST /v1/github/webhook`` with the standard GitHub App
signature header (``X-Hub-Signature-256``), verifies HMAC against the
per-installation secret, and enqueues a `Run` row when the event
matches the trigger set:

  - ``pull_request.opened``
  - ``pull_request.synchronize``
  - ``pull_request.reopened``
  - ``issue_comment.created`` whose body contains ``/cs review``

Auth model: this endpoint is intentionally NOT behind the bearer-token
auth dep because GitHub itself is the caller. Signature verification
is the only gate. Misconfigured installs (unknown `installation.id`,
bad signature, revoked install) return 401/404 so an attacker can't
distinguish "wrong secret" from "no install".

Attribution: webhook-driven runs are written under the installation's
`bot_user_id` so the team dashboard's leaderboard surfaces them as
``github-bot@<org-slug>`` instead of mis-attributing to a real user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from claudestruct.server.models import GitHubInstallation, Run, RunStatus
from claudestruct.server.webhook_metrics import WEBHOOK_ERRORS

router = APIRouter(prefix="/v1/github", tags=["github"])
log = logging.getLogger("claudestruct.server.github")


# Process-wide cache so a webhook flood doesn't mint a token per event.
# Lazy-imported below to keep this module loadable when the [server]
# extras are missing (cryptography is optional via the server extra).
_token_cache = None


def _get_token_cache():
    """Lazy singleton accessor for the install-token cache."""
    global _token_cache
    if _token_cache is None:
        from claudestruct.server.github_app import InstallationTokenCache
        _token_cache = InstallationTokenCache()
    return _token_cache


def _http_client_factory_default():
    """Lazy-import httpx so the lean install doesn't pay for it."""
    import httpx
    return httpx.Client()


def _http_client(request: Request):
    """Override hook: tests inject a fake client via
    ``app.state.github_app_http_client``. Mirrors the OAuth router's
    pattern so the test seam is consistent across modules."""
    factory = getattr(request.app.state, "github_app_http_client", None)
    if factory is None:
        return _http_client_factory_default()
    return factory()


def _ack_comment_body(*, run_id: str, trigger: str) -> str:
    """One-line acknowledgement so a reviewer can confirm the daemon
    saw the PR. The verdict comment lands in a follow-up PR; this is
    the "we're on it" receipt."""
    return (
        f":robot_face: claudeStruct queued review run `{run_id}` "
        f"(trigger: `{trigger}`). Verdict will be posted when the run completes."
    )


def _post_ack_comment_safe(
    *,
    request: Request,
    installation_id: int,
    repo_full_name: str,
    pr_number: int,
    run_id: str,
    trigger: str,
) -> None:
    """Try to post an acknowledgement comment back to the PR.

    The whole thing is best-effort: if the App isn't configured, or
    the network call fails, we log and move on — a missed comment
    must never break the webhook path that already enqueued the run.
    """
    from claudestruct.server.github_app import (
        GitHubAppError,
        load_app_config,
        post_ack_comment,
    )

    cfg = load_app_config()
    if cfg is None:
        # Pure-OSS / test deployments without App credentials skip
        # silently. The webhook still returned 202 with the run id.
        return
    client = _http_client(request)
    try:
        url = post_ack_comment(
            cfg=cfg,
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=_ack_comment_body(run_id=run_id, trigger=trigger),
            cache=_get_token_cache(),
            http_client=client,
        )
        log.info("github ack comment posted: %s", url)
    except GitHubAppError as exc:
        WEBHOOK_ERRORS.bump(
            path="github.ack_comment", reason="github_app_error",
        )
        log.warning("github ack comment failed: %s", exc)
    except Exception:  # noqa: BLE001
        # Network / unexpected errors — log loudly but do not raise.
        WEBHOOK_ERRORS.bump(
            path="github.ack_comment", reason="unexpected",
        )
        log.exception("github ack comment unexpected failure")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------


_PR_TRIGGER_ACTIONS = {"opened", "synchronize", "reopened"}
_REVIEW_COMMAND = "/cs review"


def detect_trigger(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Decide whether this event should enqueue a run.

    Returns a dict with `task`, `description`, `repo_full_name`, and
    `pr_number` when matched; ``None`` otherwise. Logic is pure so
    tests can drive the matcher without crafting full HTTP requests.
    """
    if event == "pull_request":
        action = payload.get("action")
        if action not in _PR_TRIGGER_ACTIONS:
            return None
        pr = payload.get("pull_request") or {}
        repo = (payload.get("repository") or {}).get("full_name")
        if not repo:
            return None
        title = pr.get("title") or "(no title)"
        body = pr.get("body") or ""
        # head_sha is required for the Checks API (W6.6 follow-up).
        # GitHub always populates `pull_request.head.sha` on PR events;
        # absence here would imply a malformed payload.
        head_sha = ((pr.get("head") or {}).get("sha")) or None
        base_branch = (pr.get("base") or {}).get("ref") or None
        return {
            "task": "review",
            "description": (
                f"Review PR #{pr.get('number', '?')} in {repo}: {title}\n\n"
                f"{body[:1500]}"  # cap so a 100k PR description doesn't blow context
            ).strip(),
            "repo_full_name": repo,
            "pr_number": pr.get("number"),
            "head_sha": head_sha,
            "base_branch": base_branch,
            "trigger": f"pull_request.{action}",
        }

    if event == "issue_comment":
        if payload.get("action") != "created":
            return None
        comment = payload.get("comment") or {}
        comment_body = comment.get("body") or ""
        if _REVIEW_COMMAND not in comment_body.lower():
            return None
        issue = payload.get("issue") or {}
        # Only PR comments matter; plain issues don't have diffs.
        if "pull_request" not in issue:
            return None
        repo = (payload.get("repository") or {}).get("full_name")
        if not repo:
            return None
        # The issue_comment payload doesn't carry the PR head SHA;
        # fetching it would need a separate API call. The verdict
        # comment path covers this trigger; Check runs are PR-only
        # (head_sha=None below skips the Checks-API path in the worker).
        return {
            "task": "review",
            "description": (
                f"Review PR #{issue.get('number', '?')} in {repo} (triggered by "
                f"`/cs review` comment from "
                f"@{(comment.get('user') or {}).get('login', '?')})"
            ).strip(),
            "repo_full_name": repo,
            "pr_number": issue.get("number"),
            "head_sha": None,
            "trigger": "issue_comment.cs-review",
        }

    return None


# ---------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time HMAC-SHA256 check matching GitHub's
    `X-Hub-Signature-256: sha256=<hex>` header format. Returns True
    only on exact match; rejects empty / malformed headers."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value[len("sha256=") :])


# ---------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------


@router.post(
    "/webhook",
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(
        default=None, alias="X-Hub-Signature-256",
    ),
) -> dict[str, Any]:
    """Receive a GitHub App webhook event.

    Order of operations:

    1. Read raw body so the HMAC matches GitHub's bytes exactly
       (any reformatting via Pydantic would break verification).
    2. Parse JSON to extract `installation.id`.
    3. Look up the installation row; reject unknown / revoked.
    4. Verify HMAC.
    5. Run trigger detection; if it matches, write a queued Run row
       attributed to the installation's bot user.

    Returns a small JSON body even for ignored events so a curious
    operator can sanity-check via `gh api -X POST ...` against a
    local daemon.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="webhook body must be a JSON object")

    install_id = (payload.get("installation") or {}).get("id")
    if not isinstance(install_id, int):
        # Some events (e.g. ping) do include installation; some don't.
        # Without it we can't route to an org safely.
        raise HTTPException(
            status_code=400, detail="payload missing installation.id"
        )

    factory = request.app.state.session_factory
    with factory() as session:
        install: GitHubInstallation | None = session.execute(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == install_id
            )
        ).scalar_one_or_none()
        if install is None or not install.is_active():
            # 401 (not 404) so we look the same to "wrong secret":
            # an attacker who guesses an install_id but doesn't have
            # the secret should not be able to enumerate which IDs
            # exist in our DB.
            raise HTTPException(status_code=401, detail="invalid installation or signature")

        if not verify_signature(install.webhook_secret, raw, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid installation or signature")

        # Ping events: respond OK without enqueuing.
        if x_github_event == "ping":
            return {"status": "ok", "event": "ping"}

        decision = detect_trigger(x_github_event or "", payload)
        if decision is None:
            return {
                "status": "ignored",
                "event": x_github_event,
                "reason": "no trigger match",
            }

        # Optional repo filter: substring match (case-insensitive) on
        # owner/name. Empty filter = match all.
        repo = decision["repo_full_name"]
        if install.repo_filter and install.repo_filter.lower() not in repo.lower():
            return {
                "status": "ignored",
                "event": x_github_event,
                "reason": f"repo {repo!r} does not match installation filter",
            }

        run_id = f"run-{secrets.token_hex(8)}"
        pr_number_val = decision.get("pr_number")
        head_sha_val = decision.get("head_sha")
        base_branch_val = decision.get("base_branch")
        row = Run(
            run_id=run_id,
            org_id=install.org_id,
            user_id=install.bot_user_id,
            status=RunStatus.queued.value,
            task=decision["task"],
            description=decision["description"],
            paths_json=None,
            # Persist the PR coordinates so the worker can post the
            # verdict back when the run completes (W6.6 follow-up).
            github_installation_id=install_id,
            github_repo_full_name=repo,
            github_pr_number=pr_number_val if isinstance(pr_number_val, int) else None,
            # Checks API (W6.6 follow-up): only populated for PR events.
            # NULL skips the Checks-API path in the worker without
            # breaking the comment-based verdict path.
            github_head_sha=head_sha_val if isinstance(head_sha_val, str) else None,
            # PR-open (W6.6): default branch name for the worker to fork from.
            github_base_branch=base_branch_val if isinstance(base_branch_val, str) else None,
        )
        session.add(row)
        session.commit()
        log.info(
            "github webhook enqueued run %s for org=%s repo=%s trigger=%s",
            run_id, install.org_id, repo, decision["trigger"],
        )

    # Post an "ack" comment back to the PR if the GitHub App is
    # configured. Best-effort — never block the webhook response on
    # an outbound API call (GitHub will retry if we 5xx, and a slow
    # POST here would re-trigger the whole flow).
    pr_number = decision.get("pr_number")
    if isinstance(pr_number, int):
        _post_ack_comment_safe(
            request=request,
            installation_id=install_id,
            repo_full_name=repo,
            pr_number=pr_number,
            run_id=run_id,
            trigger=decision["trigger"],
        )

    return {
        "status": "queued",
        "run_id": run_id,
        "trigger": decision["trigger"],
        "repo": repo,
    }
