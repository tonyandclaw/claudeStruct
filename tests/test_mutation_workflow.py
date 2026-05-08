"""Lock the W5.7 nightly mutation-testing slot.

These tests don't run mutmut — that's the workflow's job. They
just assert the workflow + pyproject config are wired correctly
so a future refactor can't silently dismantle the gate.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github" / "workflows" / "mutation.yml").read_text(
        encoding="utf-8",
    )


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_mutation_workflow_runs_nightly():
    """Schedule must include a cron entry — the whole point of the
    nightly slot is signal that doesn't compete with PR CI."""
    text = _workflow()
    assert "schedule:" in text
    assert "cron:" in text


def test_mutation_workflow_supports_manual_dispatch():
    """workflow_dispatch lets an operator kick off an ad-hoc run
    when reviewing a surviving mutant without waiting for the cron."""
    text = _workflow()
    assert "workflow_dispatch:" in text


def test_mutation_workflow_installs_mutation_extra():
    """The job must install the [mutation] extra — without it
    `mutmut` isn't on PATH and `mutmut run` errors out
    immediately."""
    text = _workflow()
    assert "[server,mutation]" in text or "[mutation]" in text


def test_mutation_workflow_archives_cache_for_local_replay():
    """Uploading `.mutmut-cache` is the difference between 'we ran
    a mutation suite' and 'a human can drill into a surviving
    mutant locally without re-running the suite'."""
    text = _workflow()
    assert "actions/upload-artifact" in text
    assert ".mutmut-cache" in text


def test_pyproject_declares_mutation_extra():
    """The CI step `pip install -e .[server,mutation]` must resolve
    cleanly. The extra has to be declared somewhere."""
    text = _pyproject()
    assert "mutation = [" in text
    assert "mutmut" in text


def test_pyproject_pins_mutmut_paths_to_billing_and_prompts():
    """Locking the initial scope: billing.py is critical money logic;
    prompts.py drives every LLM call. Surviving mutants in either
    are immediately interesting."""
    text = _pyproject()
    assert "[tool.mutmut]" in text
    assert "paths_to_mutate" in text
    assert "billing.py" in text
    assert "prompts.py" in text
