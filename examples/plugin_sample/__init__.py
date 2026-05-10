"""Reference plugin for the W7.4 plugin SDK.

This is an in-tree, working example. A real third-party plugin
ships as its own pip-installable package with the same shape.
The example here is what you'd write in your plugin's
``__init__.py``.

To make this discoverable via the entry-point group from a
sibling project, drop into your plugin's ``pyproject.toml``::

    [project.entry-points."claudestruct.plugins"]
    sample = "examples.plugin_sample:plugin"

(replace ``examples.plugin_sample`` with your import path).

The host then sees ``plugin`` after ``cs`` boot; its skills and
subagents are merged into the catalogue.
"""
from __future__ import annotations

from claudestruct.plugin_sdk import (
    PLUGIN_API_VERSION,
    Plugin,
    SkillContribution,
    SubagentContribution,
)

# A plugin can ship subagents — named delegated personas that the
# orchestrator can hand work off to without changing the
# Planner/Coder/Reviewer core.
_security_reviewer = SubagentContribution(
    name="security-reviewer",
    description=(
        "Reviews changes through a security lens — auth boundaries, "
        "input validation, secrets handling, dependency CVEs."
    ),
    system_prompt=(
        "You are a security reviewer. For every change, ask: who can "
        "trigger this code? what untrusted input does it consume? does "
        "it escape that input before reading from disk / network / DB? "
        "If the change touches auth, secrets, or data residency, surface "
        "any concern even if it's tangential to the diff."
    ),
    # `provider` is configuration only — the host wires it to a real
    # Provider instance at boot. This example targets Anthropic with
    # the default model, but a plugin can pin a specific model or
    # provider (`{"kind": "openai", "model": "gpt-4o"}`).
    provider={"kind": "anthropic"},
)


# Skills are short, reusable prompt fragments the host injects when
# the file glob matches. The same shape as `.claude/skills/*.md`.
_python_testing = SkillContribution(
    id="example-python-testing",
    description=(
        "Encourages tests-first edits to *.py: when adding behaviour, "
        "write the failing test before the implementation."
    ),
    body=(
        "When editing a Python file, identify the corresponding test "
        "file under tests/ first. If a test for the change doesn't "
        "exist, write one BEFORE the implementation, run the suite to "
        "confirm it fails, then implement. Do not commit a green test "
        "suite that lacks coverage for the new behaviour."
    ),
    apply_to=("*.py", "**/*.py"),
)


# The exported `plugin` is what `claudestruct.plugins` entry points
# resolve to. Keep it module-level so consumers can also import it
# directly for tests.
plugin = Plugin(
    api_version=PLUGIN_API_VERSION,
    name="claudestruct-plugin-sample",
    description=(
        "Reference plugin shipping a security-reviewer subagent + a "
        "python-testing skill. Use as a template for your own."
    ),
    subagents=(_security_reviewer,),
    skills=(_python_testing,),
)
