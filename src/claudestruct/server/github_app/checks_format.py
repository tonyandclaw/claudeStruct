"""Pure formatters for the GitHub Checks API (W6.6).

Builds JSON payloads accepted by ``POST/PATCH
/repos/{owner}/{repo}/check-runs``. No HTTP, no auth — testable
without spinning up a stub client.

Lives outside ``__init__.py`` to keep the package's main module
focused on the auth + transport concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claudestruct.server.github_app._errors import GitHubAppError

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
