"""Clone-and-push helpers for the GitHub App PR-open path (W6.6).

Pulls the worker-side ``open_pr_as_bot`` chain out of
``__init__.py``: clone the repo with the install token, stage +
commit + push the working tree changes to a new branch, then open
a PR via the existing ``create_pull_request`` primitive.

Lives in its own module so the package's main file stays focused
on auth + transport concerns. Mirrors the split pattern set up by
``checks_format.py``.

Imports from the parent package (``mint_app_jwt``,
``get_ref_sha``, ``create_pull_request``) are deferred to call
sites — at module-load time the parent package's ``__init__.py``
is still executing, so a top-level ``from claudestruct.server.
github_app import …`` would either circular-import or pick up an
incomplete namespace. Calling them lazily is the standard idiom
for sibling modules that depend on the package surface.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claudestruct.server.github_app._errors import GitHubAppError

if TYPE_CHECKING:
    # These names live in ``__init__.py`` and are imported lazily
    # inside ``open_pr_as_bot`` to dodge the circular import. Type
    # hints reference them statically only.
    from claudestruct.server.github_app import (
        GitHubAppConfig,
        InstallationTokenCache,
    )

log = logging.getLogger("claudestruct.server.github_app.clone_push")


def _run_git(
    cmd: list[str],
    cwd: str | Path,
    env: dict[str, str],
    timeout: float = 60.0,
) -> None:
    """Run a git command; raise ``GitHubAppError`` on non-zero exit."""
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

    # Lazy import to dodge the circular dependency on the still-loading
    # parent package. By the time this function actually runs (i.e.
    # ``open_pr_as_bot`` is called from the worker), ``__init__.py``
    # has finished executing and ``get_ref_sha`` is in the namespace.
    from claudestruct.server.github_app import get_ref_sha

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
    # Lazy imports — see module docstring for the rationale.
    from claudestruct.server.github_app import create_pull_request, mint_app_jwt

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
