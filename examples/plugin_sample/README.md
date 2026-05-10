# Reference plugin (`examples/plugin_sample`)

A working example of the W7.4 plugin SDK. Ship the same shape from
your own pip-installable package.

## What it contributes

- One **subagent**: `security-reviewer` — a delegated reviewer
  persona biased toward auth boundaries / input validation /
  secrets / dependency CVEs.
- One **skill**: `example-python-testing` — applies on `*.py`
  files; encourages writing the failing test before the
  implementation.

## How to register a plugin

In your plugin's `pyproject.toml`:

```toml
[project.entry-points."claudestruct.plugins"]
mine = "my_package:plugin"
```

The right-hand side resolves to a `Plugin` object exported
module-level in `my_package/__init__.py`. After `pip install
my-plugin`, `cs` discovers it via
`importlib.metadata.entry_points(group="claudestruct.plugins")`.

## Plugin contract

Mirror of `claw-squad/src/plugins.ts`. Fields:

| field         | type                              | required |
| ------------- | --------------------------------- | -------- |
| `api_version` | int (must match `PLUGIN_API_VERSION` host) | yes |
| `name`        | str (unique across loaded plugins)         | yes |
| `description` | str                                        | no  |
| `subagents`   | tuple of `SubagentContribution`            | no  |
| `skills`      | tuple of `SkillContribution`               | no  |

What plugins **cannot** do:

- Replace the dev / review / plan / debug task prompts. Those
  are core; a plugin flipping them breaks every other plugin's
  expectations.
- Hook the network call directly. Use a skill or subagent.

## Validation

The host calls `is_plugin()` and skips with a warning on:

- non-`Plugin` instance
- `api_version <= 0` or mismatched major
- empty `name`
- malformed entries inside `subagents` or `skills`

## Dedup rules

When two plugins contribute the same item:

- Duplicate plugin **name** → first one wins, second is logged.
- Duplicate **subagent name** across plugins → second drops.
- Duplicate **skill id** across plugins → second drops.

This sample uses prefixed identifiers
(`example-python-testing`) so it doesn't shadow a future
upstream skill named `python-testing`. Recommended convention.

## Trying it out

This example lives in-tree so the test suite can validate the
SDK against a real Plugin object. To use it as a real installed
plugin, copy the package to its own repo, add the entry-point
declaration, and `pip install -e .` it next to `claudestruct`.
