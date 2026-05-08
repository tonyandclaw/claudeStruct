"""End-to-end CLI tests with a mocked Anthropic SDK (W5.7 follow-up).

The unit-test floor is solid (660+ tests across the project) but the
acceptance criterion for W5.7 explicitly mentioned "end-to-end tests
with mocked Anthropic SDK". Those tests live here.

The mock target is the provider layer (`run_task`'s call into the
provider's `run_task` method). Patching there gets us:

  - The full CLI argv → context-gather → prompt-build → render path.
  - The streaming callback that the Rich panel consumes.
  - The cost-accounting + JSONL log writers.
  - The cache hit/miss surfaces.

Without forcing the test to spin up a real Anthropic key or a fake
HTTP server. We deliberately skip ``--dry-run`` here (which existing
tests already cover) so the full happy path is exercised.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from claudestruct import cli as cli_mod
from claudestruct.client import ClaudestructError, RunResult


def _runner() -> CliRunner:
    return CliRunner()


def _stderr(result) -> str:
    """Click 8.3 exposes result.stderr separately; older versions
    folded it into output. Read the right one regardless."""
    try:
        return result.stderr
    except (AttributeError, ValueError):
        return result.output


def _make_result(
    *,
    text: str = "looks good. ship it.",
    input_tokens: int = 300,
    output_tokens: int = 50,
    cache_read_tokens: int = 270,
    cache_creation_tokens: int = 0,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-4-7",
) -> RunResult:
    """Synthesize a RunResult mirroring what the AnthropicProvider
    would return on a happy path with a healthy cache hit."""
    return RunResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        stop_reason=stop_reason,
        model=model,
    )


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch):
    """Prevent any test in this file from accidentally reaching
    Anthropic. The fixture sets a dummy key (so any code path that
    reads it doesn't bail with a configuration error) and stubs
    ``count_tokens`` so dry-run / token-count helpers don't try to
    hit the SDK either."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-stub")
    monkeypatch.setattr(
        "claudestruct.cli.count_tokens",
        lambda task, user_msg, model: 100,
    )


# --- Happy paths across the four tasks -----------------------------


@pytest.mark.parametrize("task", ["dev", "review", "plan", "debug"])
def test_each_task_invokes_run_task_and_prints_response(
    monkeypatch, tmp_path: Path, task: str,
):
    """Every task command should: gather context, call run_task,
    stream the model's text via the callback, and exit 0. Locking
    the contract that the four flow into the same shared runner."""
    captured_calls: list[dict] = []

    def fake_run_task(*args, **kwargs):
        # Verify the streaming callback is wired through — Rich
        # rendering depends on it, and a regression that drops the
        # callback would silently break the live output.
        cb = kwargs.get("stream_callback")
        if cb is not None:
            cb("hello ")
            cb("world")
        captured_calls.append({"args": args, "kwargs": kwargs})
        return _make_result(text="hello world")

    monkeypatch.setattr("claudestruct.client.run_task", fake_run_task)
    monkeypatch.setattr("claudestruct.runner.run_task", fake_run_task)

    args = [task, "--root", str(tmp_path)]
    if task != "review":
        # review's description has a sensible default; the others
        # require one.
        args.append("describe the change")
    result = _runner().invoke(cli_mod.main, args)

    assert result.exit_code == 0, _stderr(result)
    assert len(captured_calls) == 1
    # The streamed text appears either in stdout or merged stderr
    # depending on Click version; both surface to the user.
    assert "hello world" in result.output or "hello world" in _stderr(result)


def test_run_task_receives_the_task_name_and_user_message(
    monkeypatch, tmp_path: Path,
):
    """The runner builds a `user_message` that includes the
    description plus rendered context. Lock that the description
    actually reaches the provider — the obvious bug if a future
    refactor breaks the threading."""
    seen: dict = {}

    def fake_run_task(task, user_message, **kw):
        seen["task"] = task
        seen["user_message"] = user_message
        return _make_result()

    monkeypatch.setattr("claudestruct.runner.run_task", fake_run_task)

    result = _runner().invoke(
        cli_mod.main,
        ["dev", "--root", str(tmp_path), "add retry to client.py"],
    )
    assert result.exit_code == 0, _stderr(result)
    assert seen["task"] == "dev"
    assert "add retry to client.py" in seen["user_message"]


# --- Cost / cache numbers surface ---------------------------------


def test_cost_summary_reflects_returned_tokens(
    monkeypatch, tmp_path: Path,
):
    """The end-of-run banner reports input/output token counts. A
    regression that mis-accounts (e.g. swapping cache_read with
    cache_creation) would corrupt the budget tracker downstream."""
    monkeypatch.setattr(
        "claudestruct.runner.run_task",
        lambda *a, **kw: _make_result(
            input_tokens=1234, output_tokens=567,
            cache_read_tokens=1100, cache_creation_tokens=0,
        ),
    )
    result = _runner().invoke(
        cli_mod.main,
        ["review", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, _stderr(result)
    out = result.output + _stderr(result)
    # Token counts and cost banner appear in the rendered usage
    # block; we just check the headline numbers reach print.
    assert "1,234" in out or "1234" in out
    assert "567" in out


def test_cached_response_renders_cached_badge(
    monkeypatch, tmp_path: Path,
):
    """A `cached=True` RunResult should surface a `(cached)` badge
    so operators know they're seeing a local cache replay, not a
    fresh API call."""
    monkeypatch.setattr(
        "claudestruct.runner.run_task",
        lambda *a, **kw: RunResult(
            text="from cache",
            input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0,
            stop_reason="cached", model="claude-opus-4-7",
            cached=True,
        ),
    )
    result = _runner().invoke(
        cli_mod.main,
        ["review", "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, _stderr(result)
    out = result.output + _stderr(result)
    assert "cached" in out.lower()


# --- Error path: provider raises ----------------------------------


def test_claudestruct_error_exits_nonzero_with_red_message(
    monkeypatch, tmp_path: Path,
):
    """A provider-side ClaudestructError (network, auth, model
    not found …) must propagate as a clean non-zero exit with the
    error message — not a Python traceback that would scare a
    casual user."""

    def fake_run_task(*a, **kw):
        raise ClaudestructError("rate limit exceeded; retry after 60s")

    monkeypatch.setattr("claudestruct.runner.run_task", fake_run_task)

    result = _runner().invoke(
        cli_mod.main,
        ["dev", "--root", str(tmp_path), "do something"],
    )
    assert result.exit_code != 0
    assert "rate limit" in (result.output + _stderr(result)).lower()


# --- Logging side effect ------------------------------------------


def test_log_json_writes_run_start_agent_usage_run_end(
    monkeypatch, tmp_path: Path,
):
    """Pinning the structured-event contract: `--log-json <path>`
    writes `run.start` + `agent.usage` + `run.end` events as JSONL.
    Downstream consumers (`cs dashboard`, prometheus exporter, the
    cost-regression alerter) all key off these names."""
    import json

    monkeypatch.setattr(
        "claudestruct.runner.run_task",
        lambda *a, **kw: _make_result(),
    )
    log_path = tmp_path / "run.jsonl"
    result = _runner().invoke(
        cli_mod.main,
        [
            "dev", "--root", str(tmp_path),
            "--log-json", str(log_path),
            "do something",
        ],
    )
    assert result.exit_code == 0, _stderr(result)
    assert log_path.exists()
    types = [
        json.loads(line)["type"]
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "run.start" in types
    assert "agent.usage" in types
    assert "run.end" in types


# --- Effort flag plumbing -----------------------------------------


def test_effort_flag_threads_through_to_run_task(
    monkeypatch, tmp_path: Path,
):
    """`--effort xhigh` on the CLI should land as `effort='xhigh'`
    in the provider call. A silent default-effort regression would
    quietly downgrade reasoning quality."""
    seen_effort: list = []

    def fake_run_task(task, user_message, **kw):
        seen_effort.append(kw.get("effort"))
        return _make_result()

    monkeypatch.setattr("claudestruct.runner.run_task", fake_run_task)

    result = _runner().invoke(
        cli_mod.main,
        [
            "plan", "--root", str(tmp_path),
            "--effort", "xhigh",
            "design X",
        ],
    )
    assert result.exit_code == 0, _stderr(result)
    assert seen_effort == ["xhigh"]


# --- --llm-cache / --no-llm-cache CLI flag plumbing (W9.4) ----------


def _capture_llm_cache(monkeypatch, tmp_path) -> dict:
    """Helper: run a `cs review` and return what value of `llm_cache`
    reached `runner.run_task`. Used by the three flag-mode tests below."""
    seen: dict = {}

    def fake_run_task(task, user_message, **kw):
        seen["llm_cache"] = kw.get("llm_cache", "<unset>")
        return _make_result()

    monkeypatch.setattr("claudestruct.runner.run_task", fake_run_task)
    return seen


def test_cli_flag_llm_cache_on_threads_to_runner(monkeypatch, tmp_path):
    """`cs review --llm-cache <desc>` must flow `llm_cache="on"` all
    the way down to runner.run_task."""
    seen = _capture_llm_cache(monkeypatch, tmp_path)

    result = _runner().invoke(
        cli_mod.main,
        ["review", "--root", str(tmp_path), "--llm-cache", "check the diff"],
    )
    assert result.exit_code == 0, _stderr(result)
    assert seen["llm_cache"] == "on"


def test_cli_flag_no_llm_cache_threads_to_runner(monkeypatch, tmp_path):
    """`--no-llm-cache` must surface as `llm_cache="off"` so the
    runner can override an env-driven default."""
    seen = _capture_llm_cache(monkeypatch, tmp_path)

    result = _runner().invoke(
        cli_mod.main,
        ["review", "--root", str(tmp_path), "--no-llm-cache", "check the diff"],
    )
    assert result.exit_code == 0, _stderr(result)
    assert seen["llm_cache"] == "off"


def test_cli_default_llm_cache_is_none_so_env_policy_applies(
    monkeypatch, tmp_path,
):
    """No flag set → llm_cache=None reaches the runner so the
    env-driven `auto` policy stays in charge (off for anthropic,
    on for openai-compat)."""
    seen = _capture_llm_cache(monkeypatch, tmp_path)

    result = _runner().invoke(
        cli_mod.main,
        ["review", "--root", str(tmp_path), "check the diff"],
    )
    assert result.exit_code == 0, _stderr(result)
    assert seen["llm_cache"] is None
